# --- bot.py: txt文件批量上传 + 去重 + /getkey ---
import os
import sys
import io
import logging
import asyncio
import asyncpg
from typing import List
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, filters
)

# 兼容 Windows 的事件循环策略（Python 3.13）
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# ---------- 工具：解析秘钥 ----------
def parse_keys_from_text(text: str) -> List[str]:
    """
    支持任意换行/空格/逗号分隔；仅保留字母数字；长度 4~64；并在本次上传内去重
    """
    import re
    if not text:
        return []
    chunks = re.split(r"[\s,;]+", text.strip())
    cleaned = []
    for s in chunks:
        s = "".join(ch for ch in s if ch.isalnum())
        if 4 <= len(s) <= 64:
            cleaned.append(s)
    # 本次去重，保留顺序
    return list(dict.fromkeys(cleaned))

# ---------- 应用启动：创建数据库连接池 ----------
async def post_init(app):
    if not DATABASE_URL:
        raise RuntimeError("缺少 DATABASE_URL 环境变量")
    app.bot_data["db_pool"] = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    logger.info("✅ 数据库连接池已建立")

# ---------- 指令 ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "你好！\n"
        "• 发送 /getkey 领取一把秘钥\n"
        "• 直接上传 .txt 文件即可批量导入（每行一个秘钥）\n"
        "• 也可用 /upload 搭配多行文本或回复消息导入"
    )

async def getkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pool = context.application.bot_data["db_pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, key_text FROM keys WHERE status='unused' "
                "ORDER BY id LIMIT 1 FOR UPDATE"
            )
            if not row:
                await update.message.reply_text("抱歉，当前没有可用秘钥。")
                return
            await conn.execute("UPDATE keys SET status='claimed' WHERE id=$1", row["id"])
            await conn.execute(
                "INSERT INTO claims(key_id, user_id, username, first_name) "
                "VALUES($1,$2,$3,$4)",
                row["id"], user.id, user.username or "", user.first_name or ""
            )
    await update.message.reply_text(f"为你分配的秘钥：{row['key_text']}")

# ---------- 文本方式导入（/upload + 多行文本 或 回复一条多行消息再发 /upload） ----------
async def upload_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # A) /upload 后面直接跟文本
    text_src = update.message.text.replace("/upload", "", 1).strip() if update.message.text else ""
    # B) 回复模式：/upload 回复到上一条多行消息
    if not text_src and update.message.reply_to_message:
        text_src = (update.message.reply_to_message.text or
                    update.message.reply_to_message.caption or "")

    keys = parse_keys_from_text(text_src)
    if not keys:
        await update.message.reply_text("没有解析到秘钥。\n"
                                        "用法：/upload 换行 多行秘钥；"
                                        "或先发多行秘钥，回复那条消息再发 /upload。")
        return
    await insert_keys_and_report(update, context, keys)

# ---------- 文件方式导入（任何 .txt 文档都会被处理，不需要写 /upload） ----------
async def upload_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        return
    # 只处理 .txt 文件
    if not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text("仅支持 .txt 文件（每行一个秘钥）。")
        return
    try:
        file = await doc.get_file()
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        text = buf.getvalue().decode("utf-8", "ignore")
        keys = parse_keys_from_text(text)
    except Exception as e:
        logger.exception("读取文件失败")
        await update.message.reply_text(f"读取文件失败：{e}")
        return

    if not keys:
        await update.message.reply_text("文件中没有解析到有效秘钥。")
        return

    await insert_keys_and_report(update, context, keys)

# ---------- 写库与反馈 ----------
async def insert_keys_and_report(update: Update, context: ContextTypes.DEFAULT_TYPE, keys: List[str]):
    total = len(keys)
    pool = context.application.bot_data["db_pool"]
    async with pool.acquire() as conn:
        # 使用 UNNEST + ON CONFLICT 去重（要求 keys.key_text 上有 UNIQUE 约束）
        rows = await conn.fetch(
            """
            WITH s AS (SELECT x FROM unnest($1::text[]) AS t(x))
            INSERT INTO keys(key_text)
            SELECT x FROM s
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            keys
        )
        inserted = len(rows)
        unused = await conn.fetchval("SELECT count(*) FROM keys WHERE status='unused'")
    skipped = total - inserted
    await update.message.reply_text(
        f"上传完成：共 {total} 条，新增 {inserted} 条，跳过 {skipped} 条。\n"
        f"当前未领：{unused}"
    )

# ---------- 主程序 ----------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("缺少 BOT_TOKEN 环境变量")
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getkey", getkey))
    app.add_handler(CommandHandler("upload", upload_text))  # 文本/回复 方式
    # 任何 .txt 文件都会触发导入
    app.add_handler(MessageHandler(filters.Document.FileExtension("txt"), upload_document))

    logger.info("🚀 机器人启动中（polling 模式）...")
    app.run_polling(allowed_updates=None)

if __name__ == "__main__":
    main()
