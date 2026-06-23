import json
from pathlib import Path


def load_task(path: str | Path) -> dict:
    content = Path(path).read_text(encoding="utf-8")
    data = json.loads(content)
    if isinstance(data, list):
        data = {"sessions": data}
    if "sessions" not in data:
        raise ValueError("Task JSON must contain 'sessions'")
    return normalize_task(data)


def _normalize_question(question: object, session_new_chat: bool, session_thinking: bool | None) -> dict:
    if isinstance(question, str):
        return {"text": question, "newChat": session_new_chat, "thinking": session_thinking}
    if isinstance(question, dict):
        return {
            "text": str(question.get("question") or question.get("text") or ""),
            "newChat": bool(question.get("newChat", session_new_chat)),
            "thinking": question.get("thinking", session_thinking),
            "options": question.get("options", {}),
        }
    return {"text": str(question), "newChat": session_new_chat, "thinking": session_thinking}


def normalize_task(data: dict) -> dict:
    sessions = data.get("sessions", [])
    if not sessions and "questions" in data:
        sessions = [{"questions": data["questions"]}]

    global_thinking = data.get("thinking", None)
    normalized_sessions: list[dict] = []
    for index, session in enumerate(sessions, start=1):
        session_new_chat = bool(session.get("newChat", True))
        session_thinking = session.get("thinking", global_thinking)
        questions = session.get("questions", [])
        normalized_questions = [_normalize_question(question, session_new_chat, session_thinking) for question in questions]
        normalized_sessions.append({
            "sessionName": session.get("sessionName") or f"session-{index}",
            "newChat": session_new_chat,
            "thinking": session_thinking,
            "questions": normalized_questions,
            "meta": session.get("meta", {}),
        })

    return {
        "taskName": data.get("taskName", "qianwen-mobile-run"),
        "mode": data.get("mode", "separate"),
        "device": data.get("device", {}),
        "thinking": global_thinking,
        "sessions": normalized_sessions,
        "options": data.get("options", {}),
        "output": data.get("output", "results/qianwen-mobile-run.json"),
    }


def summarize_task(task: dict) -> dict:
    sessions = task.get("sessions", [])
    total_questions = sum(len(session.get("questions", [])) for session in sessions)
    return {
        "taskName": task.get("taskName"),
        "mode": task.get("mode"),
        "totalSessions": len(sessions),
        "totalQuestions": total_questions,
        "sessions": [
            {
                "sessionName": session.get("sessionName"),
                "newChat": session.get("newChat"),
                "thinking": session.get("thinking"),
                "questions": [
                    {
                        "text": question.get("text"),
                        "newChat": question.get("newChat"),
                        "thinking": question.get("thinking"),
                    }
                    for question in session.get("questions", [])
                ],
            }
            for session in sessions
        ],
    }
