import asyncio
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from telegram.error import TelegramError
import json
import re
import aiohttp
from config import (BOT_TOKEN, DICTIONARY_FILE, LOGGING_LEVEL, GROUP_ID, USER_STATUS_FILE,
                    TOKENS_TO_CHECK, CHECK_INTERVAL, CONFIRMATION_CYCLES, MORALIS_API_KEY)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=getattr(logging, LOGGING_LEVEL))
logger = logging.getLogger(__name__)

ENTER_ADDRESS, CONFIRM_OVERWRITE = range(2)

# Функции для работы с словарем и файлами
def load_dictionary():
    try:
        with open(DICTIONARY_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {'user_addresses': {}, 'address_to_user': {}}

def save_dictionary(data):
    with open(DICTIONARY_FILE, 'w') as f:
        json.dump(data, f, indent=4, sort_keys=True)

dictionary = load_dictionary()
user_addresses = dictionary['user_addresses']
address_to_user = dictionary['address_to_user']

def is_valid_evm_address(address):
    return bool(re.match(r'^0x[a-fA-F0-9]{40}$', address))

# Функции для диалога
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    if user_id in user_addresses:
        await update.message.reply_text(
            f"Ваш текущий адрес: {user_addresses[user_id]}\nХотите изменить? Отправьте новый адрес или /cancel для отмены.")
        return ENTER_ADDRESS
    else:
        await update.message.reply_text("У вас нет привязанного адреса. Пожалуйста, отправьте ваш EVM адрес.")
        return ENTER_ADDRESS

async def enter_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    address = update.message.text.strip()

    if not is_valid_evm_address(address):
        await update.message.reply_text("Неверный формат адреса. Пожалуйста, отправьте корректный EVM адрес.")
        return ENTER_ADDRESS

    if address in address_to_user and address_to_user[address] != user_id:
        await update.message.reply_text(
            "Этот адрес уже привязан к другому аккаунту. Пожалуйста, используйте другой адрес.")
        return ENTER_ADDRESS

    if user_id in user_addresses:
        await update.message.reply_text(
            f"У вас уже есть привязанный адрес: {user_addresses[user_id]}\nХотите перезаписать? (да/нет)")
        context.user_data['new_address'] = address
        return CONFIRM_OVERWRITE
    else:
        user_addresses[user_id] = address
        address_to_user[address] = user_id
        save_dictionary({'user_addresses': user_addresses, 'address_to_user': address_to_user})
        await update.message.reply_text(f"Адрес успешно привязан: {address}")
        return ConversationHandler.END

async def confirm_overwrite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    response = update.message.text.lower()

    if response == 'да':
        old_address = user_addresses[user_id]
        new_address = context.user_data['new_address']

        del address_to_user[old_address]
        user_addresses[user_id] = new_address
        address_to_user[new_address] = user_id

        save_dictionary({'user_addresses': user_addresses, 'address_to_user': address_to_user})
        await update.message.reply_text(f"Адрес успешно обновлен: {new_address}")
        return ConversationHandler.END
    elif response == 'нет':
        await update.message.reply_text("Операция отменена. Ваш адрес остался без изменений.")
        return ConversationHandler.END
    else:
        await update.message.reply_text("Пожалуйста, ответьте 'да' или 'нет'.")
        return CONFIRM_OVERWRITE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# Функции для проверки токенов
def get_chain_id(chain_name):
    chain_mapping = {
        "Ethereum": "eth",
        "Base": "base",
        "Arbitrum": "arbitrum",
        "Optimism": "optimism",
    }
    return chain_mapping.get(chain_name)

async def check_token_balance(session, chain, contract, address, token_id=None):
    chain_id = get_chain_id(chain)
    if not chain_id:
        logger.error(f"Chain ID not found for chain: {chain}")
        return False

    url = f"https://deep-index.moralis.io/api/v2/{address}/nft"
    params = {
        "chain": chain_id,
        "format": "decimal",
        "token_addresses": contract,
    }
    headers = {
        "accept": "application/json",
        "X-API-Key": MORALIS_API_KEY
    }

    try:
        async with session.get(url, params=params, headers=headers) as response:
            data = await response.json()
            if "result" in data:
                for nft in data["result"]:
                    if token_id:
                        if nft["token_id"] == token_id and int(nft["amount"]) > 0:
                            return True
                    else:
                        if int(nft["amount"]) > 0:
                            return True
            return False
    except Exception as e:
        logger.error(f"Error checking token balance: {e}")
        return False

async def check_user_tokens(session, address):
    for token in TOKENS_TO_CHECK:
        parts = token.split(':')
        if len(parts) == 3:  # ERC1155
            chain, contract, token_id = parts
            if await check_token_balance(session, chain, contract, address, token_id):
                return True
        elif len(parts) == 2:  # ERC20
            chain, contract = parts
            if await check_token_balance(session, chain, contract, address):
                return True
    return False

async def update_user_statuses():
    user_data = load_dictionary()
    try:
        with open(USER_STATUS_FILE, 'r') as f:
            user_statuses = json.load(f)
    except FileNotFoundError:
        user_statuses = {}
    status_counters = user_statuses.get('counters', {})

    async with aiohttp.ClientSession() as session:
        for user_id, address in user_data['user_addresses'].items():
            is_valid = await check_user_tokens(session, address)
            current_status = user_statuses.get(user_id, 'invalid')

            if is_valid:
                status_counters[user_id] = status_counters.get(user_id, 0) + 1
                if status_counters[user_id] >= CONFIRMATION_CYCLES:
                    user_statuses[user_id] = 'valid'
                    status_counters[user_id] = CONFIRMATION_CYCLES
            else:
                status_counters[user_id] = max(0, status_counters.get(user_id, 0) - 1)
                if status_counters[user_id] == 0:
                    user_statuses[user_id] = 'invalid'

    user_statuses['counters'] = status_counters
    with open(USER_STATUS_FILE, 'w') as f:
        json.dump(user_statuses, f, indent=4, sort_keys=True)

async def check_and_remove_invalid_users(bot):
    try:
        with open(USER_STATUS_FILE, 'r') as f:
            user_statuses = json.load(f)
    except FileNotFoundError:
        logger.error(f"File {USER_STATUS_FILE} not found")
        return
    except json.JSONDecodeError:
        logger.error(f"Error decoding JSON from {USER_STATUS_FILE}")
        return

    for user_id, status in user_statuses.items():
        if user_id != 'counters':
            try:
                chat_member = await bot.get_chat_member(chat_id=GROUP_ID, user_id=int(user_id))
                if status == 'invalid' and chat_member.status not in ['left', 'kicked']:
                    await bot.ban_chat_member(chat_id=GROUP_ID, user_id=int(user_id))
                    await bot.unban_chat_member(chat_id=GROUP_ID, user_id=int(user_id))
                elif status == 'valid' and chat_member.status == 'kicked':
                    await bot.unban_chat_member(chat_id=GROUP_ID, user_id=int(user_id))
            except TelegramError as e:
                logger.error(f"Failed to check or modify user {user_id}: {e}")

async def periodic_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await update_user_statuses()
    await check_and_remove_invalid_users(context.bot)

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            ENTER_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_address)],
            CONFIRM_OVERWRITE: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_overwrite)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    application.add_handler(conv_handler)
    application.job_queue.run_repeating(periodic_check, interval=CHECK_INTERVAL, first=10)

    application.run_polling()

if __name__ == '__main__':
    main()
