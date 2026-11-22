"""Калькулятори: накопичити гроші, пенсія, капітал для дитини.
Людина відповідає на 2-3 питання числами, агент показує орієнтовний результат і кнопку запису на консультацію.
Умови розрахунку (дохідність, індексація) і всі тексти в texts.py. Відповіді зберігаються в базі (db.kv)."""
import json

import db
import texts as T

# Питання кожного калькулятора: (ключ відповіді, текст питання, мінімум, максимум)
STEPS = {
    "save": [("monthly", T.CALC_Q_MONTHLY, 100, 10_000_000), ("years", T.CALC_Q_YEARS, 1, 50)],
    "pension": [("age", T.CALC_Q_AGE, 16, 80), ("retire", T.CALC_Q_RETIRE, 30, 90),
                ("monthly", T.CALC_Q_MONTHLY, 100, 10_000_000)],
    "kids": [("child_age", T.CALC_Q_CHILD_AGE, 0, 18), ("target", T.CALC_Q_TARGET, 1, 27),
             ("monthly", T.CALC_Q_MONTHLY, 100, 10_000_000)],
}


def money(x):
    """278694 → «278 694»"""
    return f"{round(x):,}".replace(",", " ")


def grow(monthly, years):
    """Внески на початку кожного місяця, внесок щороку зростає на індексацію, дохідність нараховується щомісяця.
    Повертає (скільки внесено, скільки буде)."""
    r = T.CALC_RATE / 100 / 12
    idx = T.CALC_INDEX / 100
    paid = cap = 0.0
    c = float(monthly)
    for _ in range(years):
        for _ in range(12):
            cap = (cap + c) * (1 + r)
            paid += c
        c *= 1 + idx
    return paid, cap


def answers(uid, value=None):
    if value is None:
        try:
            return json.loads(db.kv(f"calc:{uid}") or "{}")
        except ValueError:
            return {}
    db.kv(f"calc:{uid}", json.dumps(value))
    return value


def question(kind, step):
    """Текст питання номер step (з 0) і чи є таке питання."""
    steps = STEPS[kind]
    if step >= len(steps):
        return None
    return f"{T.CALC_TITLES[kind]}\n\nКрок {step + 1} з {len(steps)}\n{steps[step][1]}"


def take(kind, step, text, uid):
    """Приймає відповідь. Повертає (помилка або "", чи це було останнє питання)."""
    key, _, lo, hi = STEPS[kind][step]
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if not digits:
        return T.CALC_NOT_NUMBER, False
    n = int(digits)
    if n < lo or n > hi:
        return T.CALC_OUT_OF_RANGE.format(lo=money(lo), hi=money(hi)), False
    a = answers(uid) if step else {}
    if key == "retire" and n <= a.get("age", 0):
        return T.CALC_RETIRE_TOO_EARLY, False
    if key == "target" and n <= a.get("child_age", 0):
        return T.CALC_TARGET_TOO_EARLY, False
    a[key] = n
    answers(uid, a)
    return "", step + 1 >= len(STEPS[kind])


def result(kind, uid):
    """Текст результату за збереженими відповідями."""
    a = answers(uid)
    if kind == "save":
        paid, cap = grow(a["monthly"], a["years"])
        return T.CALC_RES_SAVE.format(monthly=money(a["monthly"]), yearly=money(a["monthly"] * 12), years=a["years"], paid=money(paid), cap=money(cap),
                                      rate=T.CALC_RATE, index=T.CALC_INDEX)
    if kind == "pension":
        years = a["retire"] - a["age"]
        paid, cap = grow(a["monthly"], years)
        lines = "\n".join(T.CALC_PENSION_LINE.format(per_month=money(cap / (12 * n)), n=n) for n in T.CALC_PENSION_YEARS)
        return T.CALC_RES_PENSION.format(age=a["age"], retire=a["retire"], monthly=money(a["monthly"]), yearly=money(a["monthly"] * 12), years=years,
                                         paid=money(paid), cap=money(cap), lines=lines,
                                         rate=T.CALC_RATE, index=T.CALC_INDEX)
    target = a.get("target", 18)
    years = target - a["child_age"]
    paid, cap = grow(a["monthly"], years)
    return T.CALC_RES_KIDS.format(child_age=a["child_age"], target=target, monthly=money(a["monthly"]), yearly=money(a["monthly"] * 12), years=years,
                                  paid=money(paid), cap=money(cap), rate=T.CALC_RATE, index=T.CALC_INDEX)


def from_webapp(uid, raw):
    """Цифри з мінізастосунку: перевіряємо кожну і зберігаємо як звичайні відповіді. Повертає вид розрахунку або ""."""
    try:
        data = json.loads(raw or "{}")
        kind = data.get("kind", "")
        if kind not in STEPS:
            return ""
        a = {}
        for key, _, lo, hi in STEPS[kind]:
            n = int(data[key])
            if n < lo or n > hi:
                return ""
            a[key] = n
    except (ValueError, KeyError, TypeError):
        return ""
    if kind == "pension" and a["retire"] <= a["age"]:
        return ""
    if kind == "kids" and a["target"] <= a["child_age"]:
        return ""
    answers(uid, a)
    return kind
