# --- bot.py (支持所有人 /upload TXT 上传) ---
import os, sys, logging, asyncio, io
import asyncpg
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# -------- 工具：文本解析 --------
def parse_keys_from_text(text: str) -> list[str]:
    import re
    candidates = re.split(r"[\s,;]+", text or "")
    cleaned = []
    for s in candidates:
        s = "".join(ch for ch in s if ch.isalnum())
        if 4 <= len(s) <= 64:
            cleaned.append(s)
    return list(dict.fromkeys(cleaned))  # 去重

# -------- 启动时连接数据库 --------
async def post_init(app):
    app.bot_data["db_pool"] = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    logger.info("✅ 数据库连接池已建立")

# -------- 基础指令 --------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("你好！发送 /getkey 领取秘钥，或发送 /upload 上传秘钥文件。")

async def getkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pool = context.application.bot_data["db_pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, key_text FROM keys WHERE status='unused' ORDER BY id LIMIT 1 FOR UPDATE"
            )
            if not row:
                await update.message.reply_text("抱歉，当前没有可用秘钥。"); return
            await conn.execute("UPDATE keys SET status='claimed' WHERE id=$1", row["id"])
            await conn.execute(
                "INSERT INTO claims(key_id, user_id, username, first_name) VALUES($1,$2,$3,$4)",
                row["id"], user.id, user.username or "", user.first_name or ""
            )
    await update.message.reply_text(f"为你分配的秘钥：{row['key_text']}")

# -------- 上传 TXT 文件 --------
async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await update.message.reply_text("请上传一个 .txt 文件，每行一个秘钥。")
        return

    try:
        file = await update.message.document.get_file()
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        text_file = buf.getvalue().decode("utf-8", "ignore")
        keys = parse_keys_from_text(text_file)
    except Exception as e:
        logger.exception("读取文件失败")
        await update.message.reply_text(f"读取文件失败：{e}")
        return

    if not keys:
        await update.message.reply_text("文件中没有解析到有效秘钥。")
        return

    pool = context.application.bot_data["db_pool"]
    async with pool.acquire() as conn:
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

    skipped = len(keys) - inserted
    await update.message.reply_text(
        f"上传完成：共 {len(keys)} 条，新增 {inserted} 条，跳过 {skipped} 条。\n"
        f"当前未领：{unused}"
    )

# -------- 主程序 --------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getkey", getkey))
    app.add_handler(CommandHandler("upload", upload))
    logger.info("🚀 机器人启动中...")
    app.run_polling(allowed_updates=None)

if __name__ == "__main__":
    main()
