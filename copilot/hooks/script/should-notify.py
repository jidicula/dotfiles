import json
import sys


def reverse_events(path):
    with open(path, "rb") as stream:
        stream.seek(0, 2)
        position = stream.tell()
        pending = b""
        at_end = True
        while position:
            size = min(position, 65536)
            position -= size
            stream.seek(position)
            lines = (stream.read(size) + pending).split(b"\n")
            pending = lines.pop(0)
            if at_end and lines:
                lines.pop()  # Ignore a trailing incomplete write, or the final newline.
                at_end = False
            for line in reversed(lines):
                if line:
                    yield json.loads(line)
        if pending and not at_end:
            yield json.loads(pending)


path, session_id, timestamp = sys.argv[1:]
found_stop = False
for event in reverse_events(path):
    data = event.get("data", {})
    if not found_stop:
        if event.get("type") == "hook.start" and data.get("hookType") == "agentStop":
            payload = data["input"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            found_stop = (payload.get("sessionId") == session_id
                          and payload.get("timestamp") == int(timestamp))
        continue
    if data.get("parentToolCallId"):
        continue
    if event.get("type") in ("session.context_cleared", "assistant.turn_start", "user.message"):
        print("false")
        break
    if event.get("type") == "assistant.message":
        print("true" if (data.get("content", "").strip()
                         and not data.get("toolRequests")
                         and data.get("phase") in (None, "final_answer")) else "false")
        break
else:
    raise ValueError("could not identify the main-agent reply for this stop notification")
