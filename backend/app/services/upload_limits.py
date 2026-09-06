"""Bound uploaded files before materializing them in backend memory."""
from fastapi import HTTPException

MAX_AUDIO_BYTES = 50 * 1024 * 1024


async def read_upload_limited(upload, limit=MAX_AUDIO_BYTES):
    # UploadFile is already spooled by Starlette; seek avoids reading an entire
    # oversized upload into a second, unbounded in-memory buffer.
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(0)
    if size > limit:
        raise HTTPException(status_code=413, detail=f"上傳檔案不可超過 {limit // (1024 * 1024)} MiB")
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="上傳檔案過大")
    return data
