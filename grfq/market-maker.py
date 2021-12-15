"""
Description:
    Paradigm GRFQ Automated Market Maker.

    Written May 2021.

    Functionality:
    - Returns and updates two-way quotes for
      all available RFQs across all venues.
    - Adds/Drops new RFQs as created/expired.

    Design Notes:
    - Each Market Maker uses 2x Paradigm API Keys.
    -- 1x for BBO Data requests.
    -- 1x for Maker Quote submission.
    -- 2 API Keys required due to Rate Limits.

    High Level Workflow:
    - Pulls existing RFQs + Cancels existing Desk Quotes.
    - Subscribes to RFQ WS Channel to update managed RFQs to quote.
    - Updates Quote submission payloads.
    - Cancel+Replace Quotes.

Usage:
    python3 market-maker.py

Environment Variables:
    LOGGING_LEVEL - Logging Level.
    PARADIGM_MAKER_ACCOUNT_NAME - Paradigm Exchange API Key Name.
    POST_ONLY - Bool to be used to submit Quotes as Post Only.
    PODID - Kubernetes pod to use.
    SETTINGS - Quantity/Price/Refresh low+high boundaries.
    KEYS - API Keys+Secrets for Maker/BBO/Pricing Paradigm Accounts.

Requirements:
    pip3 install websockets
    pip3 install aiohttp
"""

# built ins
import asyncio
import json
import os
import traceback
import logging
import base64
import hmac
import time
from random import shuffle, uniform, randint
from typing import Dict, Tuple, List

# installed
import websockets
import aiohttp


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    )


# Primary Coroutine
async def main(
    paradigm_ws_url: str,
    paradigm_http_url: str,
    maker_account_name: str,
    post_only: str,
    contract_specifications: list,
    paradigm_maker_access_key: str,
    paradigm_maker_secret_key: str,
    paradigm_bbo_access_key: str,
    paradigm_bbo_secret_key: str,
    quote_quantity_lower_boundary: str,
    quote_quantity_higher_boundary: str,
    quote_price_tick_diff_lower_boundary: str,
    quote_price_tick_diff_higher_boundary: str,
    quote_refresh_lower_boundary: str,
    quote_refresh_higher_boundary: str
        ) -> None:
    """
    Manages multiple coroutines which:
    - Manage WS Heartbeat + RFQ WS Channel Messages.
    - Creating Quote Base Payloads.
    - Requesting BBO's for managed RFQs.
    - Updating Quote Payloads with BBOs & randomized values.
    - Ensure two-way Quotes for all managed RFQs.
    """
    # RFQs being managed
    global rfqs
    rfqs = {}
    # Quotes upon managed RFQs
    global quotes
    quotes = {}
    # RFQ Quote Books
    global quote_books
    quote_books = {}
    # Rate Limit Dict
    global rate_limit_dict
    rate_limit_dict = {}
    # Rate Limit Manager
    asyncio.get_event_loop().create_task(
        rate_limit_refiller()
        )
    # Quote Base Payload Creator Queue
    quote_base_payload_queue: asyncio.Queue = asyncio.Queue()
    # Quote Queue used for Cancel+Quote Behaviour
    quote_submission_queue: asyncio.Queue = asyncio.Queue()
    # Initiate Heartbeat + RFQ WS Channel Messages
    asyncio.get_event_loop().create_task(
        manage_ws_messages(
            ws_url=paradigm_ws_url,
            paradigm_http_url=paradigm_http_url,
            access_key=paradigm_maker_access_key,
            secret_key=paradigm_maker_secret_key,
            rfqs=rfqs,
            quotes=quotes,
            quote_base_payload_queue=quote_base_payload_queue
                )
            )
    # Create Quote Base Payloads for RFQs
    asyncio.get_event_loop().create_task(
        create_quote_base_payloads(
            rfqs=rfqs,
            quotes=quotes,
            quote_base_payload_queue=quote_base_payload_queue,
            maker_account_name=maker_account_name,
            post_only=bool(post_only)
            )
        )
    # Request the On-Screen BBO's for Quotes
    asyncio.get_event_loop().create_task(
        bbo_request_manager(
            quotes=quotes,
            access_key=paradigm_bbo_access_key,
            secret_key=paradigm_bbo_secret_key,
            paradigm_http_url=paradigm_http_url,
            contract_specifications=contract_specifications
            )
        )
    # Create Final Quote Payloads
    asyncio.get_event_loop().create_task(
        quote_final_creator(
            quotes=quotes,
            quote_submission_queue=quote_submission_queue,
            contract_specifications=contract_specifications,
            quote_quantity_lower_boundary=quote_quantity_lower_boundary,
            quote_quantity_higher_boundary=quote_quantity_higher_boundary,
            quote_price_tick_diff_lower_boundary=quote_price_tick_diff_lower_boundary,
            quote_price_tick_diff_higher_boundary=quote_price_tick_diff_higher_boundary,
            quote_refresh_lower_boundary=float(quote_refresh_lower_boundary),
            quote_refresh_higher_boundary=float(quote_refresh_higher_boundary)
        )
        )
    # Manage Quote Cancelation and Creation
    asyncio.get_event_loop().create_task(
        quote_manager(
            quotes=quotes,
            quote_submission_queue=quote_submission_queue,
            access_key=paradigm_maker_access_key,
            secret_key=paradigm_maker_secret_key,
            paradigm_http_url=paradigm_http_url
        )
        )


# Blocking Functions
def rate_limit_decorator_maker(f):
    """
    Rate Limit decorator for MAKER
    Paradigm API Key.
    """
    async def wrapper(*args, **kwargs):
        run_flag: int = 0
        while run_flag == 0:
            run_flag = await rate_limiter(
                        pdgm_account='MAKER'
                        )
            await asyncio.sleep(0)
        return await f(*args, **kwargs)
    return wrapper


def rate_limit_decorator_bbo(f):
    """
    Rate Limit decorator for BBO
    Paradigm API Key.
    """
    async def wrapper(*args, **kwargs):
        run_flag: int = 0
        while run_flag == 0:
            run_flag = await rate_limiter(
                        pdgm_account='BBO'
                        )
            await asyncio.sleep(0)
        return await f(*args, **kwargs)
    return wrapper


# Coroutines
async def quote_replace(
    access_key: str,
    secret_key: str,
    paradigm_http_url: str,
    queue: asyncio.Queue,
    quotes: Dict
        ) -> None:
    """
    Randomizes RFQ Quote side replaced
    first. Instantiates Paradigm Replace
    coroutine.
    """
    while True:
        rfq_id = await queue.get()

        sides: List = ['BUY', 'SELL']
        shuffle(sides)

        async with aiohttp.ClientSession() as session:
            tasks = []
            for side in sides:
                # Quote Payload
                body: Dict = quotes[rfq_id][side]
                body['side'] = side
                # Create Quote Client Order Id
                body['client_order_id'] = str(
                    randint(
                        0, 9999999999999999999999999999
                        )
                        )
                task: asyncio.Task = asyncio.create_task(
                        patch_rfq_rfqid_quotes(
                            session=session,
                            rfq_id=rfq_id,
                            access_key=access_key,
                            secret_key=secret_key,
                            paradigm_http_url=paradigm_http_url,
                            body=body
                        )
                    )
                tasks.append(task)
            await asyncio.gather(*tasks)
        await session.close()
        queue.task_done()


async def quote_manager(
    quotes: Dict,
    quote_submission_queue: asyncio.Queue,
    access_key: str,
    secret_key: str,
    paradigm_http_url: str
        ) -> None:
    """
    Creates Quote replace coroutine tasks
    for managed RFQs.
    """
    while True:
        await asyncio.sleep(0)
        no_actions: int = quote_submission_queue.qsize()
        if no_actions == 0:
            continue
        quotes_managed: List = []
        for quote in range(0, no_actions):
            _quote = asyncio.create_task(
                quote_replace(
                    access_key=access_key,
                    secret_key=secret_key,
                    paradigm_http_url=paradigm_http_url,
                    queue=quote_submission_queue,
                    quotes=quotes
                )
                )
            quotes_managed.append(_quote)

        await quote_submission_queue.join()

        # Cancel our worker tasks.
        for quote in quotes_managed:
            quote.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*quotes_managed, return_exceptions=True)


async def quote_final_creator(
    quotes: Dict,
    quote_submission_queue: asyncio.Queue,
    contract_specifications: Dict,
    quote_quantity_lower_boundary: str,
    quote_quantity_higher_boundary: str,
    quote_price_tick_diff_lower_boundary: str,
    quote_price_tick_diff_higher_boundary: str,
    quote_refresh_lower_boundary: float,
    quote_refresh_higher_boundary: float
        ) -> None:
    """
    Creates randomized Quote Prices.
    - Difference from venue mark price by min_tick_size.
    - Quantity randomized from block size minimum.
    - Block size minimums may not match exchange
      published block size minimums.
    """
    await asyncio.sleep(10)
    while True:
        if quotes:
            for quote in list(quotes):
                for side in ['BUY', 'SELL']:
                    for leg in range(0, len(quotes[quote]['BUY']['legs'])):
                        mark_price: float = float(quotes[quote][side]['legs'][leg]['mark_price'])
                        hedge_leg_flag: int = quotes[quote][side]['legs'][leg]['hedge_leg_flag']
                        product_code: str = quotes[quote][side]['legs'][leg]['product_code']
                        _side: str = quotes[quote][side]['legs'][leg]['side']

                        min_tick_size: float = contract_specifications[product_code]['min_tick_size']
                        dec_places: int = contract_specifications[product_code]['dec_places']
                        kind: str = contract_specifications[product_code]['kind']

                        if hedge_leg_flag == 1:
                            # No need to update Hedge leg price
                            continue

                        if _side == 'BUY':
                            # Price Randomizing
                            buy_price_tick_multiple: int = randint(
                                float(quote_price_tick_diff_lower_boundary),
                                float(quote_price_tick_diff_higher_boundary)
                                )

                            buy_price: float = mark_price - (min_tick_size * buy_price_tick_multiple)

                            if buy_price < min_tick_size:
                                buy_price = min_tick_size
                                mark_min_diff: float = mark_price - min_tick_size
                                buy_price = (1 - (buy_price_tick_multiple / 100)) * mark_min_diff

                                if buy_price < min_tick_size:
                                    buy_price = min_tick_size

                            buy_price: str = str(round(min_tick_size * round(buy_price / min_tick_size), dec_places))
                            quotes[quote][side]['legs'][leg]['price'] = buy_price
                        else:
                            sell_price_tick_multiple: int = randint(
                                float(quote_price_tick_diff_lower_boundary),
                                float(quote_price_tick_diff_higher_boundary)
                                )

                            sell_price: float = mark_price + (min_tick_size * sell_price_tick_multiple)

                            if sell_price < min_tick_size:
                                sell_price = min_tick_size * 2

                            sell_price: str = str(round(min_tick_size * round(sell_price / min_tick_size), dec_places))
                            quotes[quote][side]['legs'][leg]['price'] = sell_price

                    # Quantity Randomizing
                    min_order_size: int = contract_specifications[product_code]['min_order_size']
                    min_block_size: int = contract_specifications[product_code]['min_block_size']

                    buy_quantity_multiple: int = randint(
                            float(quote_quantity_lower_boundary),
                            float(quote_quantity_higher_boundary))
                    sell_quantity_multiple: int = randint(
                            float(quote_quantity_lower_boundary),
                            float(quote_quantity_higher_boundary))

                    if kind == 'OPTION':
                        buy_quantity = str(round(min_block_size + (buy_quantity_multiple * 50 * min_order_size), 0))
                        sell_quantity = str(round(min_block_size + (sell_quantity_multiple * 50 * min_order_size), 0))
                    elif kind == 'FUTURE':
                        buy_quantity = str(round(min_block_size + (buy_quantity_multiple * 500 * min_order_size), 0))
                        sell_quantity = str(round(min_block_size + (sell_quantity_multiple * 500 * min_order_size), 0))

                    quotes[quote]['BUY']['quantity'] = buy_quantity
                    quotes[quote]['SELL']['quantity'] = sell_quantity

                # Add to Quote Cancel / Submission Queue
                if quote_submission_queue.qsize() < len(list(quotes)):
                    quote_submission_queue.put_nowait(quote)
                else:
                    continue
        else:
            pass
        await asyncio.sleep(
            uniform(
                quote_refresh_lower_boundary,
                quote_refresh_higher_boundary
                )
                )


async def quotes_updater(
    quotes: Dict,
    requested_bbos: List,
    contract_specifications: Dict
        ) -> None:
    """
    Updates Quote Base Payloads with market data.
    """
    for bbo in range(0, len(requested_bbos)):
        id: int = requested_bbos[bbo]['id']
        if id in list(quotes):
            if 'leg_prices' not in list(requested_bbos[bbo]):
                continue
            for leg in range(0, len(requested_bbos[bbo]['leg_prices'])):
                product_code: str = quotes[id]['SELL']['legs'][leg]['product_code']
                hedge_leg_flag: int = quotes[id]['SELL']['legs'][leg]['hedge_leg_flag']
                mark_price: float = float(requested_bbos[bbo]['leg_prices'][leg]['mark_price'])

                min_tick_size: float = contract_specifications[product_code]['min_tick_size']

                if mark_price < min_tick_size:
                    mark_price = min_tick_size

                if hedge_leg_flag == 0:
                    quotes[id]['BUY']['legs'][leg]['mark_price'] = mark_price
                    quotes[id]['SELL']['legs'][leg]['mark_price'] = mark_price
                else:
                    # Hedge Leg, no need to update price.
                    continue
        else:
            continue


async def bbo_request_manager(
    quotes: Dict,
    access_key: str,
    secret_key: str,
    paradigm_http_url: str,
    contract_specifications: Dict
        ) -> None:
    """
    Requests new market data for managed RFQs.
    """
    while True:
        await asyncio.sleep(1)
        if quotes:
            rfq_bbos_to_request: List = list(quotes)
            bbos: List = []
            async with aiohttp.ClientSession() as session:
                for rfq in rfq_bbos_to_request:
                    bbos.append(asyncio.create_task(
                        get_rfq_bbo(
                            session=session,
                            paradigm_http_url=paradigm_http_url,
                            access_key=access_key,
                            secret_key=secret_key,
                            rfq_id=rfq
                            )
                        )
                    )
                requested_bbos: List = await asyncio.gather(*bbos)

                await quotes_updater(
                    quotes=quotes,
                    requested_bbos=requested_bbos,
                    contract_specifications=contract_specifications
                    )
        else:
            continue


async def create_quote_base_payloads(
    rfqs: Dict,
    quotes: Dict,
    quote_base_payload_queue: asyncio.Queue,
    maker_account_name: str,
    post_only: bool
        ) -> None:
    """
    Creates Base Quote Payload from Queue.
    - Always two-way Quotes.
    - Identifies Hedge Legs.
    """
    while True:
        _rfq: str = await quote_base_payload_queue.get()
        if _rfq in list(rfqs):
            if _rfq not in list(quotes):
                rfq: Dict = rfqs[_rfq]['legs']
                venue: str = rfqs[_rfq]['venue']
                quotes[_rfq] = {'BUY': {}, 'SELL': {}}

                # BUY Sided Payload
                quotes[_rfq]['BUY'] = {
                                       'account': maker_account_name,
                                       'legs': [],
                                       'side': 'BUY',
                                       'quantity': 0,
                                       'venue': venue,
                                       'post_only': post_only
                                       }
                # SELL Sided Payload
                quotes[_rfq]['SELL'] = {
                                        'account': maker_account_name,
                                        'legs': [],
                                        'side': 'SELL',
                                        'quantity': 0,
                                        'venue': venue,
                                        'post_only': post_only
                                        }

                for side in ['BUY', 'SELL']:
                    for leg in range(0, len(rfq)):
                        instrument: str = rfq[leg]['instrument']
                        product_code: str = rfq[leg]['product_code']
                        ratio: str = rfq[leg]['ratio']
                        _side: str = rfq[leg]['side']

                        if side == 'BUY':
                            pass
                        else:
                            if _side == 'BUY':
                                _side = 'SELL'
                            else:
                                _side = 'BUY'

                        # To be updated with bbo+randomized data
                        legs_dict: Dict = {
                                     'instrument': instrument,
                                     'price': 0,
                                     'mark_price': 0,
                                     'side': _side,
                                     'product_code': product_code,
                                     'para_bbo': None,
                                     'ratio': ratio
                                     }

                        # Managing Hedge Legs
                        if 'price' in list(rfq[leg]):
                            price: str = rfq[leg]['price']
                            legs_dict['price'] = price
                            legs_dict['hedge_leg_flag'] = 1
                        else:
                            legs_dict['hedge_leg_flag'] = 0

                        quotes[_rfq][side]['legs'].append(legs_dict)
                logging.info(f'Successfully Added RFQ Id: {_rfq} to Quote Base Payloads Hashmap')
            quote_base_payload_queue.task_done()


async def request_existing_rfqs(
    rfqs: Dict,
    queue: asyncio.Queue,
    paradigm_http_url: str,
    access_key: str,
    secret_key: str
        ) -> None:
    """
    Requests existing RFQs and updates managed RFQ Hashmap
    with the rfq_id and the leg information.

    Puts rfq_id in Queue to have the Quote Base Payload
    created.
    """
    async with aiohttp.ClientSession() as session:
        next = 1
        while next:
            response = await get_rfqs(
                    session=session,
                    paradigm_http_url=paradigm_http_url,
                    access_key=access_key,
                    secret_key=secret_key,
                    rfq_status='ACTIVE',
                    cursor=next)
            next = response['next']
            # Add RFQs to managed RFQ Hashmap
            for rfq in range(0, len(response['results'])):
                id: int = response['results'][rfq]['id']
                venue: str = response['results'][rfq]['venue']
                legs: Dict = response['results'][rfq]['legs']
                if id not in list(rfqs):
                    rfqs[id] = {'legs': legs, 'venue': venue}

                # Add RFQ to Quote Base Payload creation Queue
                queue.put_nowait(id)
    await session.close()


async def process_ws_message(
    message: str,
    rfqs: Dict,
    quotes: Dict,
    quote_base_payload_queue: asyncio.Queue
        ) -> None:
    """
    Process and store locally updates
    received from WebSocket messages.
    """
    message: Dict = json.loads(message)

    if 'params' not in message:
        return

    if 'channel' in message['params'].keys():
        # RFQ WS Channel Messages
        if message['params']['channel'] == 'rfq':
            id: int = message['params']['data']['rfq']['id']
            status: str = message['params']['data']['rfq']['status']
            legs: Dict = message['params']['data']['rfq']['legs']
            venue: str = message['params']['data']['rfq']['venue']

            if status == 'ACTIVE':
                if id not in rfqs.keys():
                    rfqs[id] = {'legs': legs, 'venue': venue}
                    logging.info(f'Adding RFQ Id to Managed: {id}')
                    quote_base_payload_queue.put_nowait(id)
                else:
                    logging.warning(f'RFQ Id: {id} should be in managed RFQ Hashmap')

            elif status == 'EXPIRED':
                if id in rfqs:
                    rfqs.pop(id)
                    logging.info(f'Removed RFQ Id: {id} from managed RFQ Hashmap')
                else:
                    logging.warning(f'RFQ ID: {id} should have been in the managed RFQ Hashmap')

                if id in quotes:
                    quotes.pop(id)
                    logging.info(f'Removed RFQ Id: {id} from Quote Payloads Hashmap')


async def manage_ws_messages(
    ws_url: str,
    paradigm_http_url: str,
    access_key: str,
    secret_key: str,
    rfqs: Dict,
    quotes: Dict,
    quote_base_payload_queue: asyncio.Queue
        ) -> None:
    """
    - Requests Existing RFQs.
    - Cancels all existing Desk Quotes.
    - Initiates the Heartbeat with Paradigm.
    - Subscribes to the RFQ WS Channel.
    - Manages messages received from RFQ WS Channel.
    """
    # Request Existing RFQs
    await request_existing_rfqs(
        rfqs=rfqs,
        queue=quote_base_payload_queue,
        paradigm_http_url=paradigm_http_url,
        access_key=access_key,
        secret_key=secret_key
        )

    # Cancel all existing Desk Quotes
    async with aiohttp.ClientSession() as session:
        tasks: List = []
        for rfq_id in rfqs:
            task: asyncio.Task = asyncio.create_task(
                delete_quotes(
                    session=session,
                    paradigm_http_url=paradigm_http_url,
                    access_key=access_key,
                    secret_key=secret_key,
                    rfq_id=rfq_id,
                    side=None
                    )
                    )
            tasks.append(task)
        await asyncio.gather(*tasks)
        await session.close()

    while True:
        try:
            # Paradigm WebSocket Connection
            async with websockets.connect(
                    f'{ws_url}?api-key={access_key}',
            ) as websocket:
                # Start Heartbeat Coroutine
                asyncio.get_event_loop().create_task(
                    send_heartbeat(
                        websocket=websocket
                        )
                    )

                # Subscribe to RFQ WS Notification Channel
                await subscribe_rfq_notification(
                    websocket=websocket
                    )

                while True:
                    # Receive WS Messages
                    message = await websocket.recv()
                    # Process WS Messages
                    await process_ws_message(
                        message=message,
                        rfqs=rfqs,
                        quotes=quotes,
                        quote_base_payload_queue=quote_base_payload_queue
                        )

        except Exception as error:
            # Allow socket to reconnect
            logging.warning('Websocket error: %s', error)


async def rate_limiter(
    pdgm_account: str
        ) -> int:
    """
    Coroutine ensures rate limit is respected.
    """
    global rate_limit_dict

    run_flag: int = 0

    if pdgm_account not in list(rate_limit_dict):
        return run_flag

    if 'remaining_amount' not in list(rate_limit_dict[pdgm_account]):
        return run_flag

    if rate_limit_dict[pdgm_account]['remaining_amount'] >= 1:
        rate_limit_dict[pdgm_account]['remaining_amount'] -= 1
        run_flag = 1
    elif rate_limit_dict[pdgm_account]['remaining_amount'] == 0:
        pass

    return run_flag


async def rate_limit_refiller() -> None:
    """
    This coroutine refills the rate limit
    buckets per Paradigm's Rate Limits.
    """
    global rate_limit_dict

    pdgm_account_rate_limits: Dict = {
        'MAKER': 30,
        'BBO': 30
        }

    remaining_refresh_boundary: float = time.time() + 1

    for pdgm_account in list(pdgm_account_rate_limits):
        if pdgm_account not in list(rate_limit_dict):
            rate_limit_dict[pdgm_account] = {}
        if 'remaining_amount' not in list(rate_limit_dict[pdgm_account]):
            rate_limit_dict[pdgm_account]['remaining_amount'] = pdgm_account_rate_limits[pdgm_account]

    while True:
        while time.time() > remaining_refresh_boundary:
            remaining_refresh_boundary = time.time() + 1

            for pdgm_account in list(pdgm_account_rate_limits):
                rate_limit_dict[pdgm_account]['remaining_amount'] = pdgm_account_rate_limits[pdgm_account]

        await asyncio.sleep(1)


# REST
async def sign_request(
    secret_key: bytes,
    method: bytes,
    path: bytes,
    body: bytes
        ) -> Tuple[str, bytes]:
    """
    Create Paradigm required RESToverHTTP signature.
    """
    signing_key: bytes = base64.b64decode(secret_key)
    timestamp: str = str(int(time.time() * 1000)).encode('utf-8')
    message: bytes = b'\n'.join([timestamp, method.upper(), path, body])
    digest: bytes = hmac.digest(signing_key, message, 'sha256')
    signature: bytes = base64.b64encode(digest)
    return timestamp, signature


@rate_limit_decorator_bbo
async def get_rfq_bbo(
    session: aiohttp.ClientSession,
    paradigm_http_url: str,
    access_key: str,
    secret_key: str,
    rfq_id: int
        ) -> Dict:
    """
    Paradigm RESToverHTTP endpoint.
    [GET] /v1/grfq/rfqs/{rfq_id}/bbo
    """
    method: str = 'GET'
    path: str = f'/v1/grfq/rfqs/{str(rfq_id)}/bbo'

    payload: str = ''

    timestamp, signature = await sign_request(
        secret_key=secret_key.encode('utf-8'),
        method=method.encode('utf-8'),
        path=path.encode('utf-8'),
        body=payload.encode('utf-8'),
        )

    headers: Dict = {
        'Paradigm-API-Timestamp': timestamp.decode('utf-8'),
        'Paradigm-API-Signature': signature.decode('utf-8'),
        'Authorization': f'Bearer {access_key}'
                    }

    async with session.get(
        paradigm_http_url+path,
        headers=headers
            ) as response:
        try:
            response: Dict = await response.json()
            response['id'] = rfq_id
        except aiohttp.ContentTypeError:
            # Paradigm very rarely returns text/html type
            response: Dict = {'id': 0}
        except aiohttp.ClientConnectionError:
            # Paradigm's Host rarely fails in name resolution
            response: Dict = {'id': 0}
        except Exception as e:
            logging.error(f'Uncaught BBO Error: {e}')
            response: Dict = {'id': 0}
        return response


@rate_limit_decorator_bbo
async def get_rfqs(
    session: aiohttp.ClientSession,
    paradigm_http_url: str,
    access_key: str,
    secret_key: str,
    rfq_status: str,
    cursor: str
        ) -> Dict:
    """
    Paradigm RESToverHTTP endpoint.
    [GET] /v1/grfq/rfqs/
    """
    method: str = 'GET'
    path: str = '/v1/grfq/rfqs/'

    if rfq_status is not None:
        path += f'?status={rfq_status}'

    if cursor is not None or 1:
        path += f'?cursor={cursor}'

    payload: str = ''

    timestamp, signature = await sign_request(
        secret_key=secret_key.encode('utf-8'),
        method=method.encode('utf-8'),
        path=path.encode('utf-8'),
        body=payload.encode('utf-8'),
        )

    headers: Dict = {
        'Paradigm-API-Timestamp': timestamp.decode('utf-8'),
        'Paradigm-API-Signature': signature.decode('utf-8'),
        'Authorization': f'Bearer {access_key}'
                    }

    async with session.get(
        paradigm_http_url+path,
        headers=headers
            ) as response:
        response: Dict = await response.json()
        return response


@rate_limit_decorator_maker
async def delete_quotes(
    session: aiohttp.ClientSession,
    paradigm_http_url: str,
    access_key: str,
    secret_key: str,
    rfq_id: int,
    side: str = None
        ) -> Tuple[int, Dict]:
    """
    Paradigm RESToverHTTP endpoint.
    [DELETE] /v1/grfq/quotes/
    """
    method: str = 'DELETE'
    path: str = '/v1/grfq/quotes/'

    path += f'?rfq_id={str(rfq_id)}'

    if side is not None:
        path += f'&side={side}'

    payload: str = ''

    timestamp, signature = await sign_request(
        secret_key=secret_key.encode('utf-8'),
        method=method.encode('utf-8'),
        path=path.encode('utf-8'),
        body=payload.encode('utf-8'),
        )

    headers: Dict = {
        'Paradigm-API-Timestamp': timestamp.decode('utf-8'),
        'Paradigm-API-Signature': signature.decode('utf-8'),
        'Authorization': f'Bearer {access_key}'
                    }

    async with session.delete(
        paradigm_http_url+path,
        headers=headers
            ) as response:
        status: int = response.status
        response: Dict = await response.json()
        return response, status


@rate_limit_decorator_maker
async def patch_rfq_rfqid_quotes(
    session: aiohttp.ClientSession,
    paradigm_http_url: str,
    access_key: str,
    secret_key: str,
    rfq_id: int,
    body: Dict
        ) -> Tuple[int, Dict]:
    """
    Paradigm RESToverHTTP endpoint.
    [PATCH] /v1/grfq/rfqs/{rfq_id}/quotes/
    """
    method: str = 'PATCH'
    path: str = f'/v1/grfq/rfqs/{str(rfq_id)}/quotes/'

    json_payload: Dict = json.dumps(body)

    timestamp, signature = await sign_request(
        secret_key=secret_key.encode('utf-8'),
        method=method.encode('utf-8'),
        path=path.encode('utf-8'),
        body=json_payload.encode('utf-8'),
        )

    headers: Dict = {
        'Paradigm-API-Timestamp': timestamp.decode('utf-8'),
        'Paradigm-API-Signature': signature.decode('utf-8'),
        'Authorization': f'Bearer {access_key}'
                    }

    async with session.patch(
        paradigm_http_url+path,
        headers=headers,
        json=body
            ) as response:
        status: int = response.status
        response: Dict = await response.json()
        return status, response


# WS
async def send_heartbeat(
    websocket: websockets.connect
        ) -> None:
    """
    Send a heartbeat message to Paradigm to
    keep the connection alive.

    Must be sent within each 10secs rolling window.
    """
    while True:
        await websocket.send(json.dumps(
            {
                "id": 1,
                "jsonrpc": "2.0",
                "method": "heartbeat"
            }
            )
            )
        await asyncio.sleep(9)


async def subscribe_rfq_notification(
    websocket: websockets.connect
        ) -> None:
    """
    Subscribe to the RFQ Channel to receive RFQ
    creation and expiration messages.
    """
    logging.info('Subscribed to RFQ WS Channel')
    await websocket.send(json.dumps(
        {
            "id": 2,
            "jsonrpc": "2.0",
            "method": "subscribe",
            "params": {
                "channel": "rfq"
                }
            }
        )
        )


if __name__ == "__main__":
    # Local Testing
    os.environ['LOGGING_LEVEL'] = 'INFO'
    os.environ['PARADIGM_MAKER_ACCOUNT_NAME'] = 'ParadigmTestNinetyFive'
    os.environ['POST_ONLY'] = 'False'
    os.environ['PODID'] = '0'
    os.environ['SETTINGS'] = '{"0": [400, 500, 1, 2, 5, 10]}'
    os.environ['KEYS'] = '''
        {"0": {"PARADIGM_MAKER_ACCESS_KEY": "Z9gBdD05yiHLotRCxrSeFTfC",
        "PARADIGM_MAKER_SECRET_KEY": "9qgG7DU0XNaqF9n5Q35iQtL5Bv7JFNUffagT7/qC9jlH0exj",
        "PARADIGM_BBO_ACCESS_KEY": "cYYmOKxNtZYYclwUfY6g3ayi",
        "PARADIGM_BBO_SECRET_KEY": "q/XGnjzyYVNXCeTU/vMhg8Z7sNpz/paQDnJN8IpKGxl6rdqM"}}
        '''

    # Paradigm Connection URLs
    paradigm_environment = os.getenv('PARADIGM_ENVIRONMENT', 'nightly')
    paradigm_ws_url: str = f'wss://ws.api.{paradigm_environment}.paradigm.co/v1/grfq/'
    paradigm_http_url: str = f'https://api.{paradigm_environment}.paradigm.co'

    ordinal_index = os.getenv('PODID')
    if not ordinal_index:
        ordinal_index = os.getenv('HOSTNAME').split('-')[-1]
    settings = json.loads(os.getenv('SETTINGS'))
    keys = json.loads(os.getenv('KEYS'))

    # Exchange Contract Specifications
    contract_specifications: dict = {
        'DO': {'min_tick_size': 0.0005, 'min_block_size': 25, 'min_order_size': 0.1, 'kind': 'OPTION', 'dec_places': 4},
        'CF': {'min_tick_size': 0.5, 'min_block_size': 20000, 'min_order_size': 10, 'kind': 'FUTURE', 'dec_places': 2},
        'EH': {'min_tick_size': 0.0005, 'min_block_size': 250, 'min_order_size': 1, 'kind': 'OPTION', 'dec_places': 4},
        'AZ': {'min_tick_size': 0.05, 'min_block_size': 100000, 'min_order_size': 1, 'kind': 'FUTURE', 'dec_places': 2},
        'BO': {'min_tick_size': 0.0001, 'min_block_size': 5, 'min_order_size': 0.1, 'kind': 'OPTION', 'dec_places': 4},
        'BF': {'min_tick_size': 0.5, 'min_block_size': 500, 'min_order_size': 10, 'kind': 'FUTURE', 'dec_places': 2},
        'VT': {'min_tick_size': 0.0001, 'min_block_size': 100, 'min_order_size': 1, 'kind': 'OPTION', 'dec_places': 4},
        'ZW': {'min_tick_size': 0.01, 'min_block_size': 5000, 'min_order_size': 1, 'kind': 'FUTURE', 'dec_places': 2},
        'GD': {'min_tick_size': 0.0005, 'min_block_size': 100, 'min_order_size': 1, 'kind': 'OPTION', 'dec_places': 4},
        'HM': {'min_tick_size': 0.05, 'min_block_size': 5000, 'min_order_size': 1, 'kind': 'FUTURE', 'dec_places': 2},
        'BB': {'min_tick_size': 0.5, 'min_block_size': 200000, 'min_order_size': 1, 'kind': 'FUTURE', 'dec_places': 2},
        'EF': {'min_tick_size': 0.05, 'min_block_size': 200000, 'min_order_size': 1, 'kind': 'FUTURE', 'dec_places': 2},
        'EO': {'min_tick_size': 0.001, 'min_block_size': 200000, 'min_order_size': 1, 'kind': 'FUTURE', 'dec_places': 4},
        'XR': {'min_tick_size': 0.0001, 'min_block_size': 200000, 'min_order_size': 1, 'kind': 'FUTURE', 'dec_places': 4},
                                    }

    # Logging
    logging.getLogger().setLevel(os.environ['LOGGING_LEVEL'].upper())

    try:
        # Start the Event Loop
        asyncio.get_event_loop().run_until_complete(
            main(
                paradigm_ws_url=paradigm_ws_url,
                paradigm_http_url=paradigm_http_url,
                maker_account_name=os.environ['PARADIGM_MAKER_ACCOUNT_NAME'],
                post_only=os.environ['POST_ONLY'],
                paradigm_maker_access_key=keys[ordinal_index]['PARADIGM_MAKER_ACCESS_KEY'],
                paradigm_maker_secret_key=keys[ordinal_index]['PARADIGM_MAKER_SECRET_KEY'],
                paradigm_bbo_access_key=keys[ordinal_index]['PARADIGM_BBO_ACCESS_KEY'],
                paradigm_bbo_secret_key=keys[ordinal_index]['PARADIGM_BBO_SECRET_KEY'],
                quote_quantity_lower_boundary=settings[ordinal_index][0],
                quote_quantity_higher_boundary=settings[ordinal_index][1],
                quote_price_tick_diff_lower_boundary=settings[ordinal_index][2],
                quote_price_tick_diff_higher_boundary=settings[ordinal_index][3],
                quote_refresh_lower_boundary=settings[ordinal_index][4],
                quote_refresh_higher_boundary=settings[ordinal_index][5],
                contract_specifications=contract_specifications
                )
                )
        asyncio.get_event_loop().run_forever()
    except Exception as e:
        print('Local Main Error')
        print(e)
        traceback.print_exc()
