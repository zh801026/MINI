# --- bot.py for Windows + PTB v20+ ---
import os
import sys
import logging
import asyncio
import asyncpg
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# Windows 事件循环策略（兼容 Python 3.13）
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 日志输出，INFO 级别（可以改成 DEBUG 看更详细的日志）
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 环境变量
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# 启动时连接数据库
async def post_init(application):
    if not DATABASE_URL:
        raise RuntimeError("缺少 DATABASE_URL 环境变量")
    application.bot_data["db_pool"] = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    logger.info("✅ 数据库连接池已建立")

# 指令：/start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("你好！发送 /getkey 领取一把秘钥。")

# 指令：/getkey
async def getkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pool = context.application.bot_data["db_pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, key_text FROM keys WHERE status='unused' ORDER BY id LIMIT 1 FOR UPDATE"
            )
            if not row:
                await update.message.reply_text("抱歉，当前没有可用秘钥。")
                return
            await conn.execute("UPDATE keys SET status='claimed' WHERE id=$1", row["id"])
            await conn.execute(
                "INSERT INTO claims(key_id, user_id, username, first_name) VALUES($1,$2,$3,$4)",
                row["id"], user.id, user.username or "", user.first_name or ""
            )
    await update.message.reply_text(f"为你分配的秘钥：{row['key_text']}")

# 指令：/remaining（管理员专用）
async def remaining(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("仅管理员可用此指令。")
        return
    pool = context.application.bot_data["db_pool"]
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM keys")
        unused = await conn.fetchval("SELECT count(*) FROM keys WHERE status='unused'")
        claimed = await conn.fetchval("SELECT count(*) FROM keys WHERE status='claimed'")
    await update.message.reply_text(f"总数:{total} 未领:{unused} 已领:{claimed}")

def main():
    if not BOT_TOKEN:
        raise RuntimeError("缺少 BOT_TOKEN 环境变量")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getkey", getkey))
    app.add_handler(CommandHandler("remaining", remaining))

    logger.info("🚀 机器人启动中（polling 模式）...")
    app.run_polling(allowed_updates=None)  # 阻塞运行

if __name__ == "__main__":
    main()
