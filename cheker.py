import asyncio
import json
import logging
import aiohttp
from config import DICTIONARY_FILE, USER_STATUS_FILE, LOGGING_LEVEL, TOKENS_TO_CHECK, ANKR_API_KEY, CHECK_INTERVAL, \
    CONFIRMATION_CYCLES

logging.basicConfig(level=getattr(logging, LOGGING_LEVEL))
logger = logging.getLogger(__name__)

ANKR_API_URL = f"https://rpc.ankr.com/multichain/{ANKR_API_KEY}"

async def load_data(file_path):
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {'user_addresses': {}, 'address_to_user': {}}

async def save_data(file_path, data):
    with open(file_path, 'w') as f:
        json.dump(data, f, indent=4, sort_keys=True)

def get_chain_id(chain_name):
    chain_mapping = {
        "Ethereum": "eth",
        "Base": "base",
        "Arbitrum": "arbitrum",
        "Optimism": "optimism",
        # Добавьте другие поддерживаемые сети здесь
    }
    return chain_mapping.get(chain_name)

async def get_nft_holders(session, chain, contract):
    chain_id = get_chain_id(chain)
    if not chain_id:
        logger.error(f"Chain ID not found for chain: {chain}")
        return set()

    payload = {
        "jsonrpc": "2.0",
        "method": "ankr_getNFTHolders",
        "params": {
            "blockchain": chain_id,
            "contractAddress": contract,
            "pageSize": 1000,
            "pageToken": ""
        },
        "id": 1
    }

    try:
        async with session.post(ANKR_API_URL, json=payload) as response:
            data = await response.json()
            logger.debug(f"API response: {data}")

            if "result" in data and "holders" in data["result"]:
                return set(holder.lower() for holder in data["result"]["holders"])
            return set()
    except Exception as e:
        logger.error(f"Error getting NFT holders: {e}")
        return set()

async def check_user_tokens(session, address, holders_cache):
    address = address.lower()
    for token in TOKENS_TO_CHECK:
        parts = token.split(':')
        logger.debug(f"Checking token: {token} for address: {address}")

        if len(parts) == 3:  # ERC1155
            chain, contract, token_id = parts
            if contract not in holders_cache:
                holders_cache[contract] = await get_nft_holders(session, chain, contract)
            if address in holders_cache[contract]:
                return True
        elif len(parts) == 2:  # ERC20 или ERC721
            chain, contract = parts
            if contract not in holders_cache:
                holders_cache[contract] = await get_nft_holders(session, chain, contract)
            if address in holders_cache[contract]:
                return True
    return False

async def update_user_statuses():
    user_data = await load_data(DICTIONARY_FILE)
    user_statuses = await load_data(USER_STATUS_FILE)
    status_counters = user_statuses.get('counters', {})
    holders_cache = {}

    async with aiohttp.ClientSession() as session:
        for user_id, address in user_data['user_addresses'].items():
            if user_id not in user_statuses:
                user_statuses[user_id] = 'invalid'
                status_counters[user_id] = 0

            is_valid = await check_user_tokens(session, address, holders_cache)
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

            logger.info(
                f"User {user_id} with address {address} status: {user_statuses[user_id]}, counter: {status_counters[user_id]}")

    user_statuses['counters'] = status_counters
    await save_data(USER_STATUS_FILE, user_statuses)
    logger.info("User statuses updated")

async def main():
    while True:
        await update_user_statuses()
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    asyncio.run(main())
