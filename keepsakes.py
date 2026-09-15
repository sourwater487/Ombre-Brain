"""Cards are views of whisper buckets; the journal contains identity, never body copies."""
import asyncio
import base64
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path


class Keepsakes:
    def __init__(self, root, buckets, delete_bucket, clean_indexes, queue_embedding, reminders=None):
        self.path = Path(root) / "keepsakes.json"
        self.media = Path(root) / ".keepsakes-media"
        self.buckets = buckets
        self.delete_bucket = delete_bucket
        self.clean_indexes = clean_indexes
        self.queue_embedding = queue_embedding
        self.lock = asyncio.Lock()
        self.reminders = reminders

    def read(self):
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(rows, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def public(self, row):
        if row["state"] in ("deleted", "deleting"):
            return None
        if row.get("reminderId"):
            memo = self.reminders.get(row["reminderId"])
            if not memo:
                return None
            return {"id": row["id"], "turnId": row["turnId"], "conversationId": row["conversationId"],
                    "type": "note", "title": "", "content": memo["content"], "createdAt": row["createdAt"],
                    "messageId": row.get("messageId"), "appearance": dict(row.get("appearance", {})),
                    "reminderId": memo["id"], "status": memo["status"], "startAt": memo["start_at"], "endAt": memo["end_at"]}
        bucket = await self.buckets.get(row["id"])
        if not bucket:
            return None
        metadata = bucket.get("metadata", {})
        if metadata.get("keepsake_id") != row["id"]:
            return None
        appearance = dict(row.get("appearance", {}))
        return {"id": row["id"], "turnId": row["turnId"],
                "conversationId": row["conversationId"], "type": row["type"],
                "title": metadata.get("keepsake_title", ""), "content": bucket["content"],
                "createdAt": metadata["created"], "messageId": row.get("messageId"), "appearance": appearance}

    async def list(self, conversation_id=None, turn_id=None):
        async with self.lock:
            result = []
            if self.reminders:
                self.reminders.archive_expired()
            for row in self.read().values():
                if conversation_id and row["conversationId"] != conversation_id:
                    continue
                if turn_id and row["turnId"] != turn_id:
                    continue
                card = await self.public(row)
                if card:
                    result.append(card)
            return sorted(result, key=lambda card: card["createdAt"], reverse=True)

    async def create(self, body):
        turn = body.get("turnId")
        conversation = body.get("conversationId")
        cards = body.get("cards")
        if not isinstance(turn, str) or not re.fullmatch(r"[A-Za-z0-9:._-]{1,180}", turn):
            raise ValueError("invalid turn")
        if not isinstance(conversation, str) or not conversation or len(conversation) > 128:
            raise ValueError("invalid conversation")
        if not isinstance(cards, list) or not 1 <= len(cards) <= 2:
            raise ValueError("每轮最多两张卡片")
        seen = set()
        for card in cards:
            if not isinstance(card, dict) or card.get("type") not in ("ramble", "note"):
                raise ValueError("卡片类型无效")
            if card["type"] in seen:
                raise ValueError("每轮同类型只能写一张")
            seen.add(card["type"])
            if not isinstance(card.get("content"), str) or not card["content"].strip() or len(card["content"]) > 20000:
                raise ValueError("卡片正文为空或过长")
            title = card.get("title", "")
            if not isinstance(title, str) or len(title) > 160 or (card["type"] == "note" and title):
                raise ValueError("便签不写标题；碎碎念标题最多160字")
            if card["type"] == "note" and self.reminders:
                for field in ("start_at", "end_at"):
                    value = card.get(field, "")
                    if not isinstance(value, str):
                        raise ValueError(field + " 必须为日期字符串")
                    self.reminders._validate_optional_time(value)
                if card.get("start_at") and card.get("end_at"):
                    start = self.reminders._parse_time(card["start_at"], now=datetime.now(timezone.utc), end_of_day=False)
                    end = self.reminders._parse_time(card["end_at"], now=datetime.now(timezone.utc), end_of_day=True)
                    if start > end:
                        raise ValueError("开始时间不能晚于到期时间")
                continue
            for field in ("valence", "arousal"):
                value = card.get(field)
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError(field + " 必须为 0–1")
        async with self.lock:
            rows = self.read()
            prepared = []
            for card in cards:
                key = "ks_" + hashlib.sha256((conversation + "\0" + turn + "\0" + card["type"]).encode()).hexdigest()[:32]
                fingerprint = hashlib.sha256(json.dumps(card, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                old = rows.get(key)
                if old and old["fingerprint"] != fingerprint:
                    raise ValueError("本轮该类型已有卡片，不能重复写入不同内容")
                prepared.append((key, fingerprint, card))
            for key, fingerprint, card in prepared:
                row = rows.get(key)
                if row and row["state"] in ("deleted", "deleting"):
                    continue
                if not row:
                    row = {"id": key, "turnId": turn, "conversationId": conversation,
                           "type": card["type"], "fingerprint": fingerprint, "state": "pending",
                           "createdAt": datetime.now(timezone.utc).isoformat()}
                    if card["type"] == "note" and self.reminders:
                        row["reminderId"] = key
                    rows[key] = row
                    self.save(rows)
                if row.get("reminderId"):
                    memo = self.reminders.get(key)
                    if not memo:
                        if row["state"] == "ready":
                            row["state"] = "deleted"
                            self.save(rows)
                            continue
                        self.reminders.create(reminder_id=key, title=card["content"].strip()[:60],
                            content=card["content"].strip(), source="keepsake_note", channel="global",
                            start_at=card.get("start_at", ""), end_at=card.get("end_at", ""))
                    if not await self.public(row):
                        raise RuntimeError("无法回查已写入的便签")
                    row["state"] = "ready"
                    self.save(rows)
                    continue
                bucket = await self.buckets.get(key)
                if not bucket:
                    if row["state"] == "ready":
                        # An externally removed memory must never be resurrected by a retry.
                        row["state"] = "deleted"
                        self.save(rows)
                        continue
                    await self.buckets.create(content=card["content"].strip(), tags=["whisper"],
                        bucket_type="feel", bucket_id=key, name=card.get("title") or None,
                        valence=card["valence"], arousal=card["arousal"], created=row["createdAt"],
                        source="chat", extra_metadata={"keepsake_id": key,
                            "keepsake_title": card.get("title", ""), "keepsake_type": card["type"],
                            "keepsake_turn": turn})
                if not await self.public(row):
                    raise RuntimeError("无法回查已写入的卡片")
                row["state"] = "ready"
                self.save(rows)
                self.queue_embedding(key)
            result = []
            for row in rows.values():
                if row["turnId"] == turn and row["conversationId"] == conversation:
                    card = await self.public(row)
                    if card:
                        result.append(card)
            return result

    async def bind(self, conversation, turn, message_id):
        if not isinstance(message_id, str) or not message_id.isdigit():
            raise ValueError("invalid message")
        async with self.lock:
            rows = self.read()
            changed = False
            for row in rows.values():
                if row["conversationId"] == conversation and row["turnId"] == turn and await self.public(row):
                    row["messageId"] = message_id
                    changed = True
            if changed:
                self.save(rows)

    async def appearance(self, card_id, appearance):
        background = appearance.get("backgroundUrl", "")
        color = appearance.get("textColor", "black")
        preview = appearance.get("previewBackgroundUrl", "")
        if not isinstance(preview, str) or len(preview) > 8192 or (preview and not re.match(r"^https?://[^\s]+$", preview)):
            raise ValueError("概览背景仅支持 HTTP(S) 图片直链")
        if not isinstance(background, str) or len(background) > 4_000_000 or color not in ("black", "white"):
            raise ValueError("卡片外观无效")
        uploaded_reference = background.startswith(f"/api/cards/{card_id}/background?v=")
        if background and not uploaded_reference and not (re.match(r"^https?://[^\s]+$", background) or re.match(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$", background)):
            raise ValueError("背景仅支持图片上传或 HTTP(S) 图片直链")
        async with self.lock:
            rows = self.read()
            row = rows.get(card_id)
            if not row or not await self.public(row):
                raise KeyError(card_id)
            previous_file = row.get("backgroundFile")
            if uploaded_reference:
                background = row.get("appearance", {}).get("backgroundUrl", "")
            elif background.startswith("data:"):
                header, encoded = background.split(",", 1)
                image_bytes = base64.b64decode(encoded, validate=True)
                if len(image_bytes) > 3_000_000:
                    raise ValueError("背景图片过大")
                version = hashlib.sha256(image_bytes).hexdigest()[:16]
                filename = f"{card_id}-{version}.image"
                self.media.mkdir(parents=True, exist_ok=True)
                temporary = self.media / (filename + ".tmp")
                with temporary.open("wb") as output:
                    output.write(image_bytes)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.media / filename)
                row["backgroundFile"] = filename
                row["backgroundMime"] = header[5:].split(";")[0]
                background = f"/api/cards/{card_id}/background?v={version}"
            else:
                row.pop("backgroundFile", None)
                row.pop("backgroundMime", None)
            row["appearance"] = {"backgroundUrl": background, "textColor": color, "previewBackgroundUrl": preview}
            self.save(rows)
            if previous_file and previous_file != row.get("backgroundFile"):
                try:
                    (self.media / previous_file).unlink(missing_ok=True)
                except OSError:
                    pass
            return await self.public(row)

    def read_global_appearance(self, kind="ramble"):
        path = self.path.with_name("notes-appearance.json" if kind == "note" else "keepsakes-appearance.json")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    async def global_appearance(self, updates=None, kind="ramble"):
        image_keys = {"dayBackgroundUrl": "day", "nightBackgroundUrl": "night", "previewBackgroundUrl": "preview"}
        color_keys = {"dayTextColor", "nightTextColor"}
        async with self.lock:
            current = self.read_global_appearance(kind)
            if updates is None:
                return {key: value for key, value in current.items() if key in image_keys or key in color_keys}
            if not isinstance(updates, dict) or set(updates) - (set(image_keys) | color_keys):
                raise ValueError("全局外观无效")
            next_style = dict(current)
            images = []
            for key, value in updates.items():
                if key in color_keys:
                    if value not in ("black", "white"):
                        raise ValueError("文字颜色无效")
                    next_style[key] = value
                    continue
                if not isinstance(value, str) or len(value) > 4_000_000:
                    raise ValueError("背景图片过大")
                slot = image_keys[key]
                prefix = f"/api/cards/{'note-appearance' if kind == 'note' else 'appearance'}/background/{slot}?v="
                if value.startswith(prefix):
                    if value != current.get(key):
                        raise ValueError("上传背景已变更，请重新打开美化")
                    continue
                if value.startswith("data:"):
                    if not re.fullmatch(r"data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+", value):
                        raise ValueError("请选择 PNG、JPEG 或 WebP 图片")
                    header, encoded = value.split(",", 1)
                    data = base64.b64decode(encoded, validate=True)
                    if len(data) > 3_000_000:
                        raise ValueError("背景图片过大")
                    version = hashlib.sha256(data).hexdigest()[:16]
                    name = f"global-{kind}-{slot}-{version}.image"
                    images.append((name, data))
                    next_style[key] = prefix + version
                    next_style[slot + "File"] = name
                    next_style[slot + "Mime"] = header[5:].split(";")[0]
                else:
                    if value and not re.fullmatch(r"https?://[^\s]+", value):
                        raise ValueError("背景仅支持图片上传或 HTTP(S) 图片直链")
                    next_style[key] = value
                    next_style.pop(slot + "File", None)
                    next_style.pop(slot + "Mime", None)
            # Validate the complete request before writing. Retain older image files.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if images:
                self.media.mkdir(parents=True, exist_ok=True)
            for name, data in images:
                temporary = self.media / (name + ".tmp")
                with temporary.open("wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.media / name)
            path = self.path.with_name("notes-appearance.json" if kind == "note" else "keepsakes-appearance.json")
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(next_style, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            return {key: value for key, value in next_style.items() if key in image_keys or key in color_keys}

    async def global_background(self, slot, kind="ramble"):
        if slot not in ("day", "night", "preview"):
            raise KeyError(slot)
        async with self.lock:
            style = self.read_global_appearance(kind)
            name = style.get(slot + "File")
            if not name:
                raise KeyError(slot)
            return style[slot + "Mime"], (self.media / name).read_bytes()

    async def background(self, card_id):
        async with self.lock:
            row = self.read().get(card_id)
            if not row or not await self.public(row):
                raise KeyError(card_id)
            filename = row.get("backgroundFile")
            if not filename:
                raise KeyError(card_id)
            return row["backgroundMime"], (self.media / filename).read_bytes()

    async def delete(self, card_id, confirm):
        if confirm != "DELETE":
            raise ValueError("需要再次确认删除")
        async with self.lock:
            rows = self.read()
            row = rows.get(card_id)
            if not row:
                raise KeyError(card_id)
            if row["state"] == "deleted":
                return
            row["state"] = "deleting"
            self.save(rows)
            if row.get("reminderId"):
                self.reminders.set_status(row["reminderId"], "archived")
                row["state"] = "deleted"
                self.save(rows)
                return
            result = await self.delete_bucket(card_id)
            if result.get("status") not in ("deleted", "not_found"):
                row["state"] = "ready"
                self.save(rows)
                raise RuntimeError("删除未完成，请重试")
            # Retry cleanup even after the bucket itself has already gone.
            _, errors = self.clean_indexes(card_id)
            if errors:
                raise RuntimeError("记忆索引清理未完成，请重试删除")
            if self.media.exists():
                for image in self.media.glob(card_id + "-*.image"):
                    image.unlink(missing_ok=True)
            row["state"] = "deleted"
            row.pop("backgroundFile", None)
            row.pop("backgroundMime", None)
            row.pop("appearance", None)
            self.save(rows)


def register_keepsakes(mcp, store, auth):
    from starlette.responses import JSONResponse

    async def appearance_endpoint(request):
        error = auth(request)
        if error:
            return error
        try:
            kind = "note" if "/note-appearance" in request.url.path else "ramble"
            if "slot" in request.path_params:
                from starlette.responses import Response
                mime, data = await store.global_background(request.path_params["slot"], kind)
                return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})
            updates = await request.json() if request.method == "PATCH" else None
            return JSONResponse({"appearance": await store.global_appearance(updates, kind)}, headers={"Cache-Control": "no-store"})
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except KeyError:
            return JSONResponse({"error": "背景不存在"}, status_code=404)
        except Exception:
            return JSONResponse({"error": "全局美化保存失败，请重试"}, status_code=503)

    mcp.custom_route("/api/keepsakes/note-appearance", methods=["GET", "PATCH"])(appearance_endpoint)
    mcp.custom_route("/api/keepsakes/note-appearance/background/{slot}", methods=["GET"])(appearance_endpoint)
    mcp.custom_route("/api/keepsakes/appearance", methods=["GET", "PATCH"])(appearance_endpoint)
    mcp.custom_route("/api/keepsakes/appearance/background/{slot}", methods=["GET"])(appearance_endpoint)

    async def endpoint(request):
        error = auth(request)
        if error:
            return error
        try:
            if request.method == "GET":
                if request.url.path.endswith("/background"):
                    from starlette.responses import Response
                    mime, data = await store.background(request.path_params["card_id"])
                    return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})
                cards = await store.list(request.query_params.get("conversationId"), request.query_params.get("turnId"))
                return JSONResponse({"cards": cards}, headers={"Cache-Control": "no-store"})
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("invalid body")
            if request.method == "POST":
                if request.url.path.endswith("/bind"):
                    await store.bind(body.get("conversationId"), body.get("turnId"), body.get("messageId"))
                    return JSONResponse({"bound": True})
                return JSONResponse({"cards": await store.create(body)})
            card_id = request.path_params["card_id"]
            if request.method == "PATCH":
                return JSONResponse({"card": await store.appearance(card_id, body)})
            await store.delete(card_id, body.get("confirm"))
            return JSONResponse({"deleted": True})
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except KeyError:
            return JSONResponse({"error": "卡片不存在"}, status_code=404)
        except Exception:
            return JSONResponse({"error": "卡片操作尚未完成，请重试"}, status_code=503)

    mcp.custom_route("/api/keepsakes/{card_id}/background", methods=["GET"])(endpoint)
    mcp.custom_route("/api/keepsakes/bind", methods=["POST"])(endpoint)
    mcp.custom_route("/api/keepsakes", methods=["GET", "POST"])(endpoint)
    mcp.custom_route("/api/keepsakes/{card_id}", methods=["PATCH", "DELETE"])(endpoint)
