"""`look` at an image in the team drive — an attachment, a saved screenshot.

    team drive bytes → one PERCEPTION model call → the answer as text

The screen's `look` (`look.py`) reads a masked screenshot through the browser tool;
this one reads a file the team already holds, so nothing leaves the database but the
model call, which goes through the model gateway like every other — budgeted, metered
and refused with the reason when vision is not set up. The image is loaded here and
dropped here: never into state, the step log or a checkpoint, the same rule the
screen's look keeps.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from runtime.domain.enums import WorkClass
from runtime.domain.errors import (
    MissingCredentials,
    ModelCallNotAllowed,
    ProviderUnavailable,
    SpecError,
)
from runtime.domain.files import (
    FILE_LOOK_PROMPT,
    FILE_LOOK_SYSTEM,
    IMAGE_TYPES,
    FileError,
    kind_of,
    team_of,
)
from runtime.domain.schemas import SCHEMAS
from runtime.domain.vision import BOT_LOOK, LookResult
from runtime.gateway.models import MAX_IMAGE_BYTES, ImageInput, ModelRequest
from runtime.graphs.common.structured import _as_object

LOOK_MAX_OUTPUT_TOKENS = 2_000


async def look_at_file(
    node: Any,
    bot: Any,
    path: str,
    question: str,
    *,
    thought: str,
    n: int,
    steps: list[str],
    answers: list[str],
    line: Callable[[int, str], str],
    say: Any,
) -> dict[str, Any]:
    action = {"type": "look", "text": question, "path": path}

    async def failed(reason: str) -> dict[str, Any]:
        await say("look", "activity", thought, {"action": action, "ok": False, "error": reason})
        steps.append(line(n, f"look at {path} failed: {reason}"))
        return {"n": n + 1, "log": steps[-12:], "answers": answers[-3:], "done": False}

    try:
        row, data = await node.org.files.blob_at(team_of(bot), path)
    except FileError as exc:
        return await failed(str(exc).splitlines()[0])
    action["path"] = row.path
    if row.media_type not in IMAGE_TYPES:
        kind = kind_of(row.media_type) if row.is_binary else "text file"
        return await failed(f"{row.path} is a {kind}, not an image; read_file reads its text")
    if len(data) > MAX_IMAGE_BYTES:
        return await failed(
            f"{row.path} is {len(data) / 1_048_576:.1f} MB; images over "
            f"{MAX_IMAGE_BYTES // 1_048_576} MB cannot be looked at"
        )

    schema = SCHEMAS.get(BOT_LOOK)
    try:
        response = await node.models.complete(
            node.ctx,
            ModelRequest(
                prompt=FILE_LOOK_PROMPT.format(
                    question=" ".join(question.split())[:600], path=row.path
                ),
                system=FILE_LOOK_SYSTEM,
                images=(
                    ImageInput(
                        media_type=row.media_type,
                        data=base64.b64encode(data).decode("ascii"),
                    ),
                ),
                max_output_tokens=LOOK_MAX_OUTPUT_TOKENS,
                metadata={"json_schema": schema.json_schema, "schema_ref": BOT_LOOK},
            ),
            work_class=WorkClass.PERCEPTION,
            call_site="bot_agent.look_file",
        )
    except (ModelCallNotAllowed, ProviderUnavailable, MissingCredentials, SpecError) as exc:
        return await failed(
            f"vision is not available ({str(exc).splitlines()[0][:200]}); ask the person "
            "what the image shows"
        )
    finally:
        del data

    payload = _as_object(response.text)
    if payload is None or schema.check(payload):
        return await failed("the vision model's answer could not be read")
    result = LookResult.model_validate(payload)
    await say("look", "activity", thought, {"action": action, "ok": True, "note": result.answer})
    answers.append(f"You looked at {row.path} and asked: {question[:200]}\n{result.answer}")
    steps.append(line(n, f"looked at {row.path}: {question[:100]}"))
    return {"n": n + 1, "log": steps[-12:], "answers": answers[-3:], "done": False}
