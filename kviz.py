"""Квіз «Яка програма підходить саме вам?» за посиланням ?start=test.
4 запитання кнопками, відповіді в базі (db.kv), результат за першою відповіддю (ціль).
Тексти в texts.py (QUIZ_...). Відправку і кнопки робить bot.py.
Назва файлу kviz.py, щоб не плутати з quiz.py із файлу «Система продажів»."""
import json

import db
import texts as T


def answers(uid, value=None):
    if value is None:
        try:
            return json.loads(db.kv(f"quiz:{uid}") or "{}")
        except ValueError:
            return {}
    db.kv(f"quiz:{uid}", json.dumps(value))
    return value


def question(i):
    """(текст, рядки кнопок) для запитання i (з 0)."""
    title, options = T.QUIZ_QS[i]
    text = T.QUIZ_Q.format(n=i + 1, total=len(T.QUIZ_QS), title=title)
    rows = [[(label, f"qz:{i}:{key}")] for key, label in options]
    return text, rows


def label(i, key):
    return dict(T.QUIZ_QS[i][1]).get(key, key)


def take(uid, i, key):
    """Зберігає відповідь. Повертає True, якщо це було останнє запитання. Неправильні дані ігнорує."""
    if not 0 <= i < len(T.QUIZ_QS) or key not in dict(T.QUIZ_QS[i][1]):
        return None
    a = answers(uid) if i else {}
    a[str(i)] = key
    answers(uid, a)
    return i + 1 >= len(T.QUIZ_QS)


def result(uid):
    """(текст результату, рядки кнопок, ключ цілі) за збереженими відповідями."""
    a = answers(uid)
    goal = a.get("0", "pension")
    text, rows = T.QUIZ_RESULTS.get(goal, T.QUIZ_RESULTS["pension"])
    extra = []
    if a.get("2") == "no":
        extra.append(T.QUIZ_NO_SAVINGS)
    extra.append(T.QUIZ_ALL_NOTE)
    return text + "\n\n" + "\n".join(extra), rows, goal


def summary(uid):
    """Короткий рядок відповідей для власниці."""
    a = answers(uid)
    return T.QUIZ_SUMMARY.format(*(label(i, a.get(str(i), "")) for i in range(len(T.QUIZ_QS))))
