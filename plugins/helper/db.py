"""
Database layer (MongoDB via motor).

Collections:
  users             -> one doc per user, including any pending force-sub join requests
  files             -> one doc per shareable link: code -> stored message refs
  settings          -> runtime-editable admin settings (see settings.py wrapper)
  pending_deletions -> auto-delete jobs that must survive a bot restart
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

import motor.motor_asyncio
from bson import ObjectId
from bson.errors import InvalidId

from config import DB_URL, DB_NAME

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, uri: str, database_name: str):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.users = self.db.users
        self.files = self.db.files
        self.settings = self.db.settings
        self.pending_deletions = self.db.pending_deletions

    # ================= Users ================= #
    def _new_user(self, user_id: int) -> Dict:
        return {
            "_id": int(user_id),
            "join_date": datetime.utcnow(),
            "last_seen": datetime.utcnow(),
            "join_requests": {},  # {channel_id_str: requested_at}
            "banned": False,
        }

    async def add_user(self, user_id: int) -> bool:
        """Insert the user if they're new, else just bump last_seen.
        Returns True the first time a given user_id is ever seen, so
        callers can fire "new user" notifications without a separate
        is_user_exist() round-trip."""
        if not await self.is_user_exist(user_id):
            await self.users.insert_one(self._new_user(user_id))
            return True
        await self.users.update_one(
            {"_id": int(user_id)}, {"$set": {"last_seen": datetime.utcnow()}}
        )
        return False

    async def is_user_exist(self, user_id: int) -> bool:
        return bool(await self.users.find_one({"_id": int(user_id)}))

    async def total_users_count(self) -> int:
        return await self.users.count_documents({})

    async def get_all_user_ids(self) -> List[int]:
        return [u["_id"] async for u in self.users.find({}, {"_id": 1})]

    async def is_banned(self, user_id: int) -> bool:
        u = await self.users.find_one({"_id": int(user_id)})
        return bool(u and u.get("banned"))

    # ---- join-request tracking (for "request to join" force-sub channels) ---- #
    async def record_join_request(self, user_id: int, channel_id: int):
        await self.users.update_one(
            {"_id": int(user_id)},
            {"$set": {f"join_requests.{channel_id}": datetime.utcnow()}},
            upsert=True,
        )

    async def has_pending_join_request(self, user_id: int, channel_id: int) -> bool:
        u = await self.users.find_one(
            {"_id": int(user_id)}, {f"join_requests.{channel_id}": 1}
        )
        return bool(u and str(channel_id) in {str(k) for k in u.get("join_requests", {})})

    async def clear_join_request(self, user_id: int, channel_id: int):
        await self.users.update_one(
            {"_id": int(user_id)}, {"$unset": {f"join_requests.{channel_id}": ""}}
        )

    # ================= Files / shareable links ================= #
    async def create_file_link(self, code: str, doc: Dict) -> bool:
        doc["_id"] = code
        doc.setdefault("created_at", datetime.utcnow())
        doc.setdefault("views", 0)
        try:
            await self.files.insert_one(doc)
            return True
        except Exception as e:
            logger.error(f"create_file_link failed: {e}")
            return False

    async def get_file_link(self, code: str) -> Optional[Dict]:
        return await self.files.find_one({"_id": code})

    async def delete_file_link(self, code: str) -> bool:
        result = await self.files.delete_one({"_id": code})
        return result.deleted_count > 0

    async def increment_views(self, code: str):
        await self.files.update_one({"_id": code}, {"$inc": {"views": 1}})

    async def total_links_count(self) -> int:
        return await self.files.count_documents({})

    async def total_files_stored(self) -> int:
        pipeline = [{"$project": {"n": {"$size": "$messages"}}},
                    {"$group": {"_id": None, "total": {"$sum": "$n"}}}]
        agg = await self.files.aggregate(pipeline).to_list(1)
        return agg[0]["total"] if agg else 0

    # ================= Settings (raw KV store) ================= #
    async def save_setting(self, key: str, value):
        await self.settings.update_one(
            {"_id": key},
            {"$set": {"value": value, "updated_at": datetime.utcnow()}},
            upsert=True,
        )

    async def get_setting(self, key: str, default=None):
        doc = await self.settings.find_one({"_id": key})
        return doc["value"] if doc else default

    async def get_all_settings(self) -> Dict:
        return {doc["_id"]: doc["value"] async for doc in self.settings.find({})}

    # ================= Pending auto-deletions ================= #
    # BUGFIX: add_pending_deletion() used to return str(result.inserted_id),
    # while the document's real "_id" field in Mongo is still an ObjectId.
    # remove_pending_deletion() then did delete_one({"_id": <that string>}),
    # which never matches an ObjectId — so completed auto-delete jobs were
    # NEVER cleaned up from pending_deletions. On every restart,
    # restore_pending_deletions() would reload every one of those stale
    # "completed" jobs (their delete_at already in the past, so they'd try
    # to re-delete messages that were already gone) alongside genuinely
    # pending ones, and the collection would grow forever. We now keep the
    # id as a real ObjectId end-to-end, and remove_pending_deletion()
    # tolerates either an ObjectId or a string that looks like one.
    async def add_pending_deletion(self, doc: Dict) -> ObjectId:
        result = await self.pending_deletions.insert_one(doc)
        return result.inserted_id

    async def get_all_pending_deletions(self) -> List[Dict]:
        return [d async for d in self.pending_deletions.find({})]

    async def remove_pending_deletion(self, _id):
        if not isinstance(_id, ObjectId):
            try:
                _id = ObjectId(str(_id))
            except (InvalidId, TypeError):
                logger.warning(f"remove_pending_deletion got a non-ObjectId id: {_id!r}")
                return
        await self.pending_deletions.delete_one({"_id": _id})


# Single shared instance used by every plugin.
db = Database(DB_URL, DB_NAME)
