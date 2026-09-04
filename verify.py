#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка результатов розыгрыша Achiefy.

Скрипт заново вычисляет победителей по опубликованным файлам snapshot.json и
result.json и сравнивает их с объявленными. Ничего, кроме стандартной библиотеки
Python, не требуется.

    python verify.py --snapshot snapshot.json --result result.json

Подробности и описание алгоритма — в README.md.
"""

import argparse
import codecs
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_API = "https://mempool.space/api"
BACKUP_API = "https://blockstream.info/api"

# Сколько блоков перед выигрышным проверяется на «ещё до момента случайности».
BOUNDARY_DEPTH = 6

OK = "OK "
FAIL = "FAIL"


# --------------------------------------------------------------------------------------
# Алгоритм (полностью повторяет опубликованный код розыгрыша)
# --------------------------------------------------------------------------------------

def derive_number(block_hash, place):
    """r = SHA256(blockHash + ":" + место) как беззнаковое число (big-endian)."""
    payload = "{0}:{1}".format(block_hash.strip().lower(), place).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest(), "big")


def select_winners(participants, block_hash, places, excluded):
    """
    Разыгрывает указанные места среди участников с билетами, кроме исключённых.

    Участники сортируются по publicId по возрастанию, их билеты выкладываются в
    одну линию, и место достаётся тому, в чей диапазон попало r. Победитель
    выбывает из розыгрыша следующих мест — один участник получает один приз.
    """
    eligible = sorted(
        (p for p in participants
         if int(p["tickets"]) > 0 and p["publicId"] not in excluded),
        key=lambda p: p["publicId"],
    )

    winners = {}
    for place in sorted(places):
        if not eligible:
            break
        remaining = sum(int(p["tickets"]) for p in eligible)
        r = derive_number(block_hash, place) % remaining
        cumulative = 0
        for candidate in eligible:
            cumulative += int(candidate["tickets"])
            if r < cumulative:
                winners[place] = candidate["publicId"]
                eligible.remove(candidate)
                break
    return winners


def replay(snapshot, result):
    """
    Проигрывает все раунды по порядку и возвращает итоговое распределение мест.

    Раунд переролла разыгрывает только свои места. Из розыгрыша исключаются
    дисквалифицированные аккаунты и действующие победители остальных мест —
    они свои призы сохраняют.
    """
    participants = snapshot["participants"]
    standing = {}
    rounds = sorted(result.get("rounds", []), key=lambda r: r["round"])

    for rnd in rounds:
        places = [int(p) for p in rnd["places"]]
        excluded = set(e["publicId"] for e in rnd.get("excluded", []))
        excluded.update(pid for place, pid in standing.items() if place not in places)
        standing.update(select_winners(participants, rnd["blockHash"], places, excluded))

    return standing


# --------------------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------------------

def read_bytes(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    # Некоторые редакторы дописывают BOM; на хеш файла это влиять не должно.
    return raw[3:] if raw.startswith(codecs.BOM_UTF8) else raw


def load_json(path):
    return json.loads(read_bytes(path).decode("utf-8"))


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def parse_utc(value):
    """Разбирает время из JSON (с 'Z', со смещением или без) как UTC."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:  # доли секунды бывают длиннее, чем понимает fromisoformat
        head, _, tail = text.partition(".")
        digits = ""
        for char in tail:
            if char.isdigit():
                digits += char
            else:
                tail = tail[len(digits):]
                break
        else:
            tail = ""
        text = head + "." + digits[:6] + tail
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def short(public_id):
    return public_id[:8] + "…" if len(public_id) > 8 else public_id


def fetch(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": "achiefy-verifier"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8").strip()


# --------------------------------------------------------------------------------------
# Проверки
# --------------------------------------------------------------------------------------

def check_snapshot_hash(snapshot_bytes, result, report):
    declared = result.get("snapshotSha256")
    actual = sha256_hex(snapshot_bytes)
    print("SHA-256 снимка: {0}".format(actual))
    if not declared:
        report.warn("в result.json нет поля snapshotSha256 — сверить файлы автоматически нельзя")
        return
    if declared.lower() == actual:
        report.ok("snapshot.json — тот самый файл, по которому считался результат")
    else:
        report.fail("snapshot.json НЕ соответствует result.json (ожидался {0})".format(declared))


def check_totals(snapshot, report):
    participants = snapshot["participants"]
    tickets = sum(int(p["tickets"]) for p in participants)
    ids = [p["publicId"] for p in participants]

    if len(set(ids)) != len(ids):
        report.fail("в снимке есть повторяющиеся ID участников")
    else:
        report.ok("ID участников уникальны ({0} шт.)".format(len(ids)))

    declared_tickets = snapshot.get("totalTickets")
    if declared_tickets is not None and int(declared_tickets) != tickets:
        report.fail("сумма билетов {0} не совпадает с заявленной {1}".format(tickets, declared_tickets))
    else:
        report.ok("сумма билетов сходится: {0}".format(tickets))


def check_rounds_metadata(result, report):
    """Каждый переролл обязан быть объявлен ДО момента своей случайности."""
    for rnd in sorted(result.get("rounds", []), key=lambda r: r["round"]):
        number = rnd["round"]
        randomness_after = parse_utc(rnd["randomnessAfterUtc"])
        committed = parse_utc(rnd["committedUtc"])

        if committed >= randomness_after:
            report.fail("раунд {0}: состав зафиксирован не раньше момента случайности".format(number))
        else:
            report.ok("раунд {0}: состав зафиксирован до момента случайности".format(number))

        announced = rnd.get("announcedUtc")
        if number > 1:
            if not announced:
                report.fail("раунд {0}: переролл не объявлялся публично".format(number))
            elif parse_utc(announced) >= randomness_after:
                report.fail("раунд {0}: объявление вышло позже момента случайности".format(number))
            else:
                report.ok("раунд {0}: объявлен до момента случайности".format(number))


def check_block_online(rnd, api, report):
    """Сверяет блок раунда с публичным обозревателем Bitcoin."""
    number = rnd["round"]
    height = rnd.get("blockHeight")
    declared_hash = (rnd.get("blockHash") or "").lower()
    threshold = parse_utc(rnd["randomnessAfterUtc"]).timestamp()

    if not height or height <= 0:
        report.warn("раунд {0}: высота блока не указана, онлайн-проверка пропущена".format(number))
        return

    try:
        actual_hash = fetch("{0}/block-height/{1}".format(api, height)).lower()
        block = json.loads(fetch("{0}/block/{1}".format(api, actual_hash)))
    except (urllib.error.URLError, ValueError, OSError) as error:
        report.warn("раунд {0}: не удалось обратиться к {1} ({2})".format(number, api, error))
        return

    if actual_hash != declared_hash:
        report.fail("раунд {0}: на высоте {1} в сети другой блок ({2})".format(number, height, actual_hash))
        return
    report.ok("раунд {0}: блок #{1} существует и его hash совпадает".format(number, height))

    if int(block["timestamp"]) < threshold:
        report.fail("раунд {0}: блок старше момента случайности".format(number))
        return
    report.ok("раунд {0}: блок появился после объявленного момента".format(number))

    # Правило «наименьшая высота с timestamp ≥ момента»: ни один из предыдущих
    # блоков не должен подходить под условие.
    too_early = []
    for previous in range(int(height) - BOUNDARY_DEPTH, int(height)):
        try:
            previous_hash = fetch("{0}/block-height/{1}".format(api, previous))
            previous_block = json.loads(fetch("{0}/block/{1}".format(api, previous_hash)))
        except (urllib.error.URLError, ValueError, OSError):
            report.warn("раунд {0}: не удалось проверить блок {1}".format(number, previous))
            return
        if int(previous_block["timestamp"]) >= threshold:
            too_early.append(previous)

    if too_early:
        report.fail("раунд {0}: блоки {1} подходили раньше — взят не первый".format(number, too_early))
    else:
        report.ok("раунд {0}: более ранних подходящих блоков нет — взят первый".format(number))


def compare(standing, result, report):
    published = {int(w["place"]): w["publicId"] for w in result.get("standing", [])}
    prizes = {int(w["place"]): w.get("prize", "") for w in result.get("standing", [])}

    print("")
    print("Место | Приз                       | Победитель | Совпадает")
    print("------+----------------------------+------------+----------")
    for place in sorted(set(list(published) + list(standing))):
        expected = published.get(place)
        computed = standing.get(place)
        match = "да" if expected == computed else "НЕТ"
        print("{0:>5} | {1:<26} | {2:<10} | {3}".format(
            place, prizes.get(place, "—")[:26], short(computed or expected or "—"), match))
    print("")

    if published == standing:
        report.ok("все {0} мест совпали с опубликованными".format(len(published)))
    else:
        for place in sorted(set(list(published) + list(standing))):
            if published.get(place) != standing.get(place):
                report.fail("место {0}: объявлен {1}, пересчёт даёт {2}".format(
                    place, published.get(place, "—"), standing.get(place, "—")))


class Report(object):
    def __init__(self):
        self.failures = 0
        self.warnings = 0

    def ok(self, message):
        print("  [ok]   {0}".format(message))

    def warn(self, message):
        self.warnings += 1
        print("  [!]    {0}".format(message))

    def fail(self, message):
        self.failures += 1
        print("  [ОШИБКА] {0}".format(message))


# --------------------------------------------------------------------------------------
# Самопроверка
# --------------------------------------------------------------------------------------

def self_test():
    """Контрольные векторы: если они сходятся, скрипт считает так же, как розыгрыш."""
    failures = []

    # 1. Вывод числа из hash блока (значение зафиксировано и в коде розыгрыша).
    expected = "bfcf0b9cbe9d8208b2cddd9a01c31a9a60698a106a0e4fc1664233a15f16acec"
    if format(derive_number("abc", 1), "x") != expected:
        failures.append("derive_number('abc', 1) не совпал с контрольным вектором")

    # 2. Розыгрыш = проход по диапазонам билетов в порядке сортировки ID.
    participants = [
        {"publicId": "aaaa", "tickets": 7},
        {"publicId": "bbbb", "tickets": 5},
        {"publicId": "cccc", "tickets": 9},
    ]
    block = "00000000000000000001a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3"
    r = derive_number(block, 1) % 21
    manual = "aaaa" if r < 7 else "bbbb" if r < 12 else "cccc"
    if select_winners(participants, block, [1], set()) != {1: manual}:
        failures.append("выбор победителя разошёлся с ручным проходом по диапазонам")

    # 3. Порядок участников во входных данных ни на что не влияет.
    if select_winners(participants, block, [1, 2], set()) != \
            select_winners(list(reversed(participants)), block, [1, 2], set()):
        failures.append("результат зависит от порядка участников в файле")

    # 4. Переролл: место 2 разыгрывается заново, место 1 остаётся за своим владельцем.
    snapshot = {"participants": participants + [{"publicId": "dddd", "tickets": 3}]}
    first = {"round": 1, "places": [1, 2], "blockHash": block, "excluded": [],
             "committedUtc": "2026-09-15T18:00:00Z", "randomnessAfterUtc": "2026-09-15T18:05:00Z"}
    round_one = replay(snapshot, {"rounds": [first]})
    loser = round_one[2]
    second = {"round": 2, "places": [2], "blockHash": block[:-2] + "ff",
              "excluded": [{"publicId": loser, "reason": "бот"}],
              "committedUtc": "2026-09-15T19:00:00Z", "randomnessAfterUtc": "2026-09-15T19:30:00Z"}
    final = replay(snapshot, {"rounds": [first, second]})
    if final[1] != round_one[1]:
        failures.append("переролл сместил победителя места, которое не переигрывалось")
    if final[2] == loser:
        failures.append("исключённый участник снова получил приз")
    if final[2] == final[1]:
        failures.append("один участник получил два приза")

    for failure in failures:
        print("  [ОШИБКА] {0}".format(failure))
    if failures:
        print("\nСамопроверка НЕ пройдена.")
        return 1
    print("  [ok]   контрольные векторы сошлись")
    print("  [ok]   переролл ведёт себя по правилам")
    print("\nСамопроверка пройдена: скрипт считает так же, как розыгрыш.")
    return 0


# --------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Проверка результатов розыгрыша Achiefy по публичным файлам.")
    parser.add_argument("--snapshot", help="путь к snapshot.json")
    parser.add_argument("--result", help="путь к result.json")
    parser.add_argument("--online", action="store_true",
                        help="дополнительно сверить блоки Bitcoin через публичный обозреватель")
    parser.add_argument("--api", default=DEFAULT_API,
                        help="адрес обозревателя для --online (по умолчанию {0})".format(DEFAULT_API))
    parser.add_argument("--self-test", action="store_true",
                        help="прогнать контрольные векторы без файлов")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if not args.snapshot or not args.result:
        parser.error("нужны --snapshot и --result (или --self-test)")

    snapshot_bytes = read_bytes(args.snapshot)
    snapshot = json.loads(snapshot_bytes.decode("utf-8"))
    result = load_json(args.result)
    report = Report()

    print("Кампания:     {0}".format(result.get("campaign", snapshot.get("campaign", "—"))))
    print("Статус:       {0}".format(
        "результаты утверждены" if result.get("status") == "approved" else "предварительные результаты"))
    print("Участников:   {0}".format(len(snapshot["participants"])))
    print("Билетов:      {0}".format(sum(int(p["tickets"]) for p in snapshot["participants"])))
    print("Раундов:      {0}".format(len(result.get("rounds", []))))
    print("")

    print("Файлы")
    check_snapshot_hash(snapshot_bytes, result, report)
    check_totals(snapshot, report)

    print("")
    print("Честность раундов")
    check_rounds_metadata(result, report)

    if args.online:
        print("")
        print("Блоки Bitcoin ({0})".format(args.api))
        for rnd in sorted(result.get("rounds", []), key=lambda r: r["round"]):
            check_block_online(rnd, args.api.rstrip("/"), report)

    print("")
    print("Пересчёт победителей")
    standing = replay(snapshot, result)
    compare(standing, result, report)

    if report.failures:
        print("РЕЗУЛЬТАТ: НЕ СОШЛОСЬ — {0} расхождений.".format(report.failures))
        return 1
    if report.warnings:
        print("РЕЗУЛЬТАТ: сошлось, но {0} проверок пропущено (см. [!] выше).".format(report.warnings))
        return 0
    print("РЕЗУЛЬТАТ: всё сошлось. Розыгрыш проведён по опубликованным правилам.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
