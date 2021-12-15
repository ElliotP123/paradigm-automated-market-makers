"""
Description:
    Paradigm FSPD Automated Market Maker.

    Written June 2021.

    Functionality:
    - Provides the number of quotes per Strategy,
    per side as per specified in the environment
    variable.

    Design Notes:
    - Uses entirely Paradigm's API for everything
    (data + actions).

    High Level Workflow:
    - Pull existing Strategies and subscribes to requisite
    WebSocket Channels.
    - Creates Order Payloads + Submits Orders.
    - Refreshes Orders.

Usage:
    python3 market-maker.py

Environment Variables:
    PARADIGM_ENVIRONMENT - Paradigm Operating Environment. 'TEST'.
    LOGGING_LEVEL - Logging Level. 'INFO'.
    PARADIGM_MAKER_ACCOUNT_NAME - Paradigm Venue API Key Name.
    PARADIGM_MAKER_ACCESS_KEY - Paradgim Maker Access Key.
    PARADIGM_MAKER_SECRET_KEY - Paradigm Maker Secret Key.
    ORDER_NUMBER_PER_SIDE - Number of Orders to maintain per side of each Strategy.
    QUOTE_QUANTITY_LOWER_BOUNDARY - Lower Bound No. ticks from Block Size Minimum.
    QUOTE_QUANTITY_HIGHER_BOUNDARY - Upper Bound No. ticks from Block Size Minimum.
    QUOTE_PRICE_LOWER_BOUNDARY - Lower Bound No. ticks from Minimum Tick Size.
    QUOTE_PRICE_HIGHER_BOUNDARY - Upper Bound No. ticks from Minimum Tick Size.
    QUOTE_REFRESH_LOWER_BOUNDARY - Lower Bound Quote Refresh in seconds.
    QUOTE_REFRESH_HIGHER_BOUNDARY - Upper Bound Quote Refresh in seconds.

Requirements:
    pip3 install websockets
    pip3 install aiohttp
"""

# built ins
import asyncio
import json
import os
import sys
import traceback
import logging
import base64
import hmac
import time
from random import uniform, randint, shuffle
from typing import Dict, List, Tuple
import math

# installed
import websockets
import aiohttp


# Primary Coroutine
async def main(
    paradigm_ws_url: str,
    paradigm_http_url: str,
    paradigm_maker_account_name: str,
    paradigm_maker_access_key: str,
    paradigm_maker_secret_key: str,
    order_number_per_side: str,
    quote_quantity_lower_boundary: str,
    quote_quantity_higher_boundary: str,
    quote_price_tick_diff_lower_boundary: str,
    quote_price_tick_diff_higher_boundary: str,
    quote_refresh_lower_boundary: str,
    quote_refresh_higher_boundary: str
        ) -> None:
    """
    Primary async coroutine.
    """
    # Global Variables
    global loop
    loop = None
    global rate_limit_dict
    rate_limit_dict = {}
    global managed_strategies
    managed_strategies = {}
    global strategy_order_payloads
    strategy_order_payloads = {}
    global orders_to_submit
    orders_to_submit = []
    global orders_to_replace
    orders_to_replace = []

    loop = asyncio.get_event_loop()

    # Rate Limit Manager
    loop.create_task(
        rate_limit_refiller()
        )

    # Manage WebSocket Messages
    loop.create_task(
        manage_ws_messages(
            paradigm_maker_access_key=paradigm_maker_access_key,
            paradigm_maker_secret_key=paradigm_maker_secret_key,
            paradigm_account_name=paradigm_maker_account_name,
            order_number_per_side=int(
                order_number_per_side
                )
        )
        )

    # Order Management
    loop.create_task(
        order_manager(
            paradigm_maker_access_key=paradigm_maker_access_key,
            paradigm_maker_secret_key=paradigm_maker_secret_key,
            order_number_per_side=int(
                order_number_per_side
                ),
            quote_quantity_lower_boundary=int(
                quote_quantity_lower_boundary
                ),
            quote_quantity_higher_boundary=int(
                quote_quantity_higher_boundary
                ),
            quote_price_tick_diff_lower_boundary=int(
                quote_price_tick_diff_lower_boundary
                ),
            quote_price_tick_diff_higher_boundary=int(
                quote_price_tick_diff_higher_boundary
                ),
            quote_refresh_lower_boundary=float(
                quote_refresh_lower_boundary
                ),
            quote_refresh_higher_boundary=float(
                quote_refresh_higher_boundary
            )
        )
        )


# Coroutines
async def manage_ws_messages(
    paradigm_maker_access_key: str,
    paradigm_maker_secret_key: str,
    paradigm_account_name: str,
    order_number_per_side: int
        ) -> None:
    """
    Manages incoming WS messages as well as
    on initialization pulls all available
    Strategies and stores them locally.
    """
    global managed_strategies
    global strategy_order_payloads

    # Cancel all Desk Orders
    await delete_orders(
        paradigm_maker_access_key=paradigm_maker_access_key,
        paradigm_maker_secret_key=paradigm_maker_secret_key,
        )
    await asyncio.sleep(5)
    # Pull all available Strategies
    strategies: Dict = await get_strategies(
        paradigm_maker_access_key=paradigm_maker_access_key,
        paradigm_maker_secret_key=paradigm_maker_secret_key
        )
    # Ingest available Strategies
    await ingest_strategies(
        strategies=strategies,
        order_number_per_side=order_number_per_side,
        paradigm_account_name=paradigm_account_name
        )

    # WebSocket Subscriptions
    async with websockets.connect(
            f'{paradigm_ws_url}?api-key={paradigm_maker_access_key}'
    ) as websocket:
        # Start the Heartbeat Task
        loop.create_task(
            send_heartbeat(
                websocket=websocket
                )
            )
        # Subscribe to the `strategy_state.{venue}.{kind}` WS Channel
        await subscribe_strategy_state_notifications(
            websocket=websocket
            )
        # Subscribe to the `orders.{venue}.{kind}.{strategy_id}` WS Channel
        await subscribe_orders_notifications(
            websocket=websocket
            )
        # Subscribe to the `venue_bbo.{strategy_id}` WS Channel
        await subscribe_venue_bbo_notification(
            websocket=websocket
            )

        message_channel: str = None

        while True:
            try:
                # Receive WebSocket messages
                message = await websocket.recv()
                message = json.loads(message)

                if 'id' in list(message):
                    message_id: int = message['id']
                    if message_id == 1:
                        # Heartbeat Reponse
                        continue
                    else:
                        channel_subscribed: str = message['result']['channel']
                        logging.info(f'Successfully Subscribed to WebSocket Channel: {channel_subscribed}')
                else:
                    message_channel = message['params']['channel']

                # `strategy_stage.{venue}.{kind}` Messages
                if message_channel == 'strategy_state.ALL.ALL':
                    state: str = message['params']['data']['state']
                    strategy_id: str = message['params']['data']['id']

                    # Ingest New Strategies
                    if state == 'ACTIVE':
                        # Pull strategies endpoint for all the deets
                        strategies: Dict = await get_strategies(
                            strategy_id=strategy_id
                            )
                        # Ingest New Strategies
                        await ingest_strategies(
                            strategies=strategies,
                            order_number_per_side=order_number_per_side,
                            paradigm_account_name=paradigm_account_name
                            )
                    else:
                        # Exgest Settled/Expired Strategies
                        await exgest_strategies(
                            strategy_id=strategy_id,
                            order_number_per_side=order_number_per_side
                            )
                # `orders.{venue}.{kind}.{strategy_id}` Messages
                elif message_channel == 'orders.ALL.ALL.ALL':
                    order_label: str = message['params']['data']['label']
                    order_id: str = message['params']['data']['id']
                    state: str = message['params']['data']['state']

                    if order_label in list(strategy_order_payloads):
                        if state == 'OPEN':
                            try:
                                strategy_order_payloads[order_label]['order_id'].append(order_id)
                            except KeyError:
                                dog = strategy_order_payloads[order_label]
                                print('ne')

                            _order_ids = strategy_order_payloads[order_label]['order_id']
                        elif state == 'CLOSED':
                            if order_id in strategy_order_payloads[order_label]['order_id']:
                                strategy_order_payloads[order_label]['order_id'].remove(order_id)
                # `venue_bbo.{strategy_id}` Messages
                elif message_channel == 'venue_bbo.ALL':
                    strategy_id: str = message['params']['data']['id']
                    min_price: str = message['params']['data']['min_price']
                    max_price: str = message['params']['data']['max_price']
                    mark_price: str = message['params']['data']['mark_price']
                    best_bid_price: str = message['params']['data']['best_bid_price']
                    best_ask_price: str = message['params']['data']['best_ask_price']

                    if strategy_id in managed_strategies:
                        managed_strategies[strategy_id]['min_price'] = min_price
                        managed_strategies[strategy_id]['max_price'] = max_price
                        managed_strategies[strategy_id]['mark_price'] = mark_price
                        managed_strategies[strategy_id]['best_bid_price'] = best_bid_price
                        managed_strategies[strategy_id]['best_ask_price'] = best_ask_price

                await asyncio.sleep(0)
            except Exception as e:
                logging.info(f'WebSocket Connection Issue: {e}')
                sys.exit(1)


async def ingest_strategies(
    strategies: Dict,
    order_number_per_side: int,
    paradigm_account_name: str
        ) -> None:
    """
    Ingests the response of the get_strategies()
    coroutine.
    """
    global managed_strategies
    global strategy_order_payloads

    no_strategies: int = len(strategies)
    for strategy in range(0, no_strategies):
        strategy_id: str = strategies[strategy]['id']
        venue: str = strategies[strategy]['venue']
        min_order_size: float = strategies[strategy]['min_order_size']
        min_tick_size: float = float(strategies[strategy]['min_tick_size'])
        min_block_size: float = strategies[strategy]['min_block_size']

        managed_strategies[strategy_id] = {
            'venue': venue,
            'min_order_size': min_order_size,
            'min_tick_size': min_tick_size,
            'min_block_size': min_block_size,
            'min_price': None,
            'max_price': None,
            'mark_price': None,
            'best_bid_price': None,
            'best_ask_price': None
            }

        # Creating Order Payloads
        for order in range(0, order_number_per_side):
            for side in ['BUY', 'SELL']:
                label: str = f'{strategy_id}-{side}-{order}'
                if label not in list(strategy_order_payloads):
                    strategy_order_payloads[label] = {
                        'side': side,
                        'strategy_id': strategy_id,
                        'price': None,
                        'amount': None,
                        'type': 'LIMIT',
                        'label': label,
                        'time_in_force': 'GOOD_TILL_CANCELED',
                        'account_name': paradigm_account_name,
                        'order_id': []
                        }


async def exgest_strategies(
    strategy_id: str,
    order_number_per_side: int
        ) -> None:
    """
    Exgests Strategies and Order Payloads
    from local stores.
    """
    global managed_strategies
    global strategy_order_payloads

    # Remove from the managed_strategies dict
    managed_strategies.pop(strategy_id)

    # Remove Orders from strategy_order_payloads dict
    for order in range(0, order_number_per_side):
        for side in ['BUY', 'SELL']:
            label: str = f'{strategy_id}-{side}-{order}'
            if label in list(strategy_order_payloads):
                strategy_order_payloads.pop(label)


async def rate_limiter(
    venue: str,
    rate_limit_increment: int
        ) -> int:
    """
    Function ensures rate limit is respected.

    Paradigm's default rate limit is
    200 requests per second per account.
    """
    global rate_limit_dict

    run_flag: int = 0

    if venue not in list(rate_limit_dict):
        return run_flag

    if 'remaining_amount' not in list(rate_limit_dict[venue]):
        return run_flag

    if rate_limit_dict[venue]['remaining_amount'] >= 1:
        rate_limit_dict[venue]['remaining_amount'] -= rate_limit_increment
        run_flag = 1
    elif rate_limit_dict[venue]['remaining_amount'] == 0:
        pass

    return run_flag


async def rate_limit_refiller() -> None:
    """
    This coroutine refills the rate limit
    buckets per the venue's Rate Limit convention.
    """
    global rate_limit_dict

    venue_rate_limits: Dict = {
        'PDGM': 200
        }

    remaining_refresh_boundary: float = time.time() + 1

    for venue in list(venue_rate_limits):
        if venue not in list(rate_limit_dict):
            rate_limit_dict[venue] = {}
        if 'remaining_amount' not in list(rate_limit_dict[venue]):
            rate_limit_dict[venue]['remaining_amount'] = venue_rate_limits[venue]

    while True:
        while time.time() > remaining_refresh_boundary:
            remaining_refresh_boundary = time.time() + 1

            for venue in list(venue_rate_limits):
                rate_limit_dict[venue]['remaining_amount'] = venue_rate_limits[venue]

        await asyncio.sleep(1)


async def order_creator(
    order_number_per_side: int,
    quote_quantity_lower_boundary: str,
    quote_quantity_higher_boundary: str,
    quote_price_tick_diff_lower_boundary: str,
    quote_price_tick_diff_higher_boundary: str
        ) -> None:
    """
    Creates Order Payloads
    """
    global strategy_order_payloads
    global orders_to_submit
    global orders_to_replace

    for strategy in list(managed_strategies):
        for order in range(0, order_number_per_side):
            for side in ['BUY', 'SELL']:
                label: str = f'{strategy}-{side}-{order}'

                # Update Amount
                min_order_size: int = managed_strategies[strategy]['min_order_size']
                min_block_size: int = managed_strategies[strategy]['min_block_size']

                random_multiple: int = randint(
                    quote_quantity_lower_boundary,
                    quote_quantity_higher_boundary
                    )

                base_amount: int = min_block_size if min_block_size > min_order_size else min_order_size
                total_amount: int = base_amount + (random_multiple * min_order_size)

                strategy_order_payloads[label]['amount'] = total_amount

                # Update Price
                min_tick_size: int = managed_strategies[strategy]['min_tick_size']

                if managed_strategies[strategy]['min_price'] is None:
                    continue
                min_price: float = float(managed_strategies[strategy]['min_price'])
                max_price: float = float(managed_strategies[strategy]['max_price'])
                mark_price: float = float(managed_strategies[strategy]['mark_price'])
                best_bid_price: float = float(managed_strategies[strategy]['best_bid_price'])
                best_ask_price: float = float(managed_strategies[strategy]['best_ask_price'])

                # Price just off the Mark Price if both price variables are set to 0
                price_at_mark_flag: int = 1 if quote_price_tick_diff_lower_boundary == 0 \
                    and quote_price_tick_diff_higher_boundary == 0 else 0

                random_price_multiple: int = randint(
                            quote_price_tick_diff_lower_boundary,
                            quote_price_tick_diff_higher_boundary)

                if price_at_mark_flag == 0:
                    if side == 'BUY':
                        price: float = mark_price - (random_price_multiple * (order+2 * min_tick_size))
                    elif side == 'SELL':
                        price: float = mark_price + (random_price_multiple * (order+2 * min_tick_size))
                else:
                    if side == 'BUY':
                        price: float = mark_price - (random_price_multiple * (order * min_tick_size))
                    elif side == 'SELL':
                        price: float = mark_price + (random_price_multiple * (order * min_tick_size))

                # Ensure Price is within the min_tick_size increments
                price: str = str(round_nearest(price, min_tick_size))

                strategy_order_payloads[label]['price'] = price

                if strategy_order_payloads[label]['order_id']:
                    if len(strategy_order_payloads[label]['order_id']) == 1:
                        # Append to orders_to_replace list
                        orders_to_replace.append(label)
                    else:
                        continue
                else:
                    # Append to orders_to_submit list
                    orders_to_submit.append(label)

    # Randomize Order of Actions for more randomness
    shuffle(orders_to_submit)
    shuffle(orders_to_replace)


async def order_manager(
    paradigm_maker_access_key: str,
    paradigm_maker_secret_key: str,
    order_number_per_side: int,
    quote_quantity_lower_boundary: int,
    quote_quantity_higher_boundary: int,
    quote_price_tick_diff_lower_boundary: int,
    quote_price_tick_diff_higher_boundary: int,
    quote_refresh_lower_boundary: float,
    quote_refresh_higher_boundary: float
        ) -> None:
    """
    Manages Orders.
    """
    global strategy_order_payloads
    global orders_to_submit
    global orders_to_replace

    while True:
        # Create Order Payloads
        # Create Randomized Price and Order Amount Values
        await order_creator(
            order_number_per_side=order_number_per_side,
            quote_quantity_lower_boundary=quote_quantity_lower_boundary,
            quote_quantity_higher_boundary=quote_quantity_higher_boundary,
            quote_price_tick_diff_lower_boundary=quote_price_tick_diff_lower_boundary,
            quote_price_tick_diff_higher_boundary=quote_price_tick_diff_higher_boundary
            )

        # Submit + Replace Orders
        tasks: List = []
        # Orders to Submit
        for label in orders_to_submit:
            submission = loop.create_task(
                post_orders(
                    paradigm_maker_access_key=paradigm_maker_access_key,
                    paradigm_maker_secret_key=paradigm_maker_secret_key,
                    payload=strategy_order_payloads[label]
                    )
                )
            _price: float = strategy_order_payloads[label]['price']
            _amount: float = strategy_order_payloads[label]['amount']
            _side: float = strategy_order_payloads[label]['side']
            logging.debug(f'Submitting Order | {label} | Side: {_side} | Price: {_price} | Amount: {_amount}')
            tasks.append(submission)
        orders_to_submit = []
        # Orders to Replace
        for label in orders_to_replace:
            replace = loop.create_task(
                post_orders_orderid_replace(
                    paradigm_maker_access_key=paradigm_maker_access_key,
                    paradigm_maker_secret_key=paradigm_maker_secret_key,
                    order_id=strategy_order_payloads[label]['order_id'][0],
                    payload=strategy_order_payloads[label]
                    )
                )
            _price: float = strategy_order_payloads[label]['price']
            _amount: float = strategy_order_payloads[label]['amount']
            _side: float = strategy_order_payloads[label]['side']
            logging.debug(f'Replacing Order | {label} | Side: {_side} | Price: {_price} | Amount: {_amount}')
            tasks.append(replace)
        orders_to_replace = []

        try:
            await asyncio.gather(*tasks)
        except aiohttp.ServerDisconnectedError as e:
            logging.info(e)
            sys.exit(1)

        await asyncio.sleep(uniform(quote_refresh_lower_boundary, quote_refresh_higher_boundary))


# Blocking Functions
def round_nearest(x, a):
    """
    Used to round random prices to the
    correct min_tick_size of the Strategy.
    """
    return round(round(x / a) * a, -int(math.floor(math.log10(a))))


def rate_limit_decorator(f):
    async def wrapper(*args, **kwargs):
        run_flag: int = 0
        while run_flag == 0:
            run_flag = await rate_limiter(
                        venue='PDGM',
                        rate_limit_increment=1
                        )
            await asyncio.sleep(0)
        return await f(*args, **kwargs)
    return wrapper


# RESToverHTTP Interface
async def sign_request(
    paradigm_maker_secret_key: str,
    method: str,
    path: str,
    body: Dict
        ) -> Tuple[int, bytes]:
    """
    Creates the required signature neccessary
    as apart of all RESToverHTTP requests with Paradigm.
    """
    _secret_key: bytes = paradigm_maker_secret_key.encode('utf-8')
    _method: bytes = method.encode('utf-8')
    _path: bytes = path.encode('utf-8')
    _body: bytes = body.encode('utf-8')
    signing_key: bytes = base64.b64decode(_secret_key)
    timestamp: str = str(int(time.time() * 1000)).encode('utf-8')
    message: bytes = b'\n'.join([timestamp, _method.upper(), _path, _body])
    digest: hmac.digest = hmac.digest(signing_key, message, 'sha256')
    signature: bytes = base64.b64encode(digest)

    return timestamp, signature


async def create_rest_headers(
    paradigm_maker_access_key: str,
    paradigm_maker_secret_key: str,
    method: str,
    path: str,
    body: Dict
        ) -> Dict:
    """
    Creates the required headers to authenticate
    Paradigm RESToverHTTP requests.
    """
    timestamp, signature = await sign_request(
        paradigm_maker_secret_key=paradigm_maker_secret_key,
        method=method,
        path=path,
        body=body
        )

    headers: Dict = {
        'Paradigm-API-Timestamp': timestamp.decode('utf-8'),
        'Paradigm-API-Signature': signature.decode('utf-8'),
        'Authorization': f'Bearer {paradigm_maker_access_key}'
        }

    return headers


@rate_limit_decorator
async def get_strategies(
    paradigm_maker_access_key: str,
    paradigm_maker_secret_key: str,
    strategy_id: str = None
        ) -> Dict:
    """
    Paradigm RESToverHTTP endpoint.
    [GET] /strategies
    """
    method: str = 'GET'
    path: str = '/v1/fs/strategies?page_size=100'
    payload: str = ''

    if strategy_id is not None:
        path += f'&id={strategy_id}'

    headers: Dict = await create_rest_headers(
        paradigm_maker_access_key=paradigm_maker_access_key,
        paradigm_maker_secret_key=paradigm_maker_secret_key,
        method=method,
        path=path,
        body=payload
        )

    async with aiohttp.ClientSession() as session:
        async with session.get(
            paradigm_http_url+path,
            headers=headers
                ) as response:
            status_code: int = response.status
            response: Dict = await response.json()
            if status_code != 200:
                message: str = 'Unable to [GET] /strategies'
                if strategy_id is not None:
                    message += f'?id={strategy_id}'
                logging.error(message)
                logging.error(f'Status Code: {status_code}')
                logging.error(f'Response Text: {response}')
            response = response['results']
    return response


@rate_limit_decorator
async def delete_orders(
    paradigm_maker_access_key: str,
    paradigm_maker_secret_key: str,
        ) -> None:
    """
    Paradigm RESToverHTTP endpoint.
    [DELETE] /orders
    """
    method: str = 'DELETE'
    path: str = '/v1/fs/orders'
    payload: str = ''

    headers: Dict = await create_rest_headers(
        paradigm_maker_access_key=paradigm_maker_access_key,
        paradigm_maker_secret_key=paradigm_maker_secret_key,
        method=method,
        path=path,
        body=payload
        )

    async with aiohttp.ClientSession() as session:
        async with session.delete(
            paradigm_http_url+path,
            headers=headers
                ) as response:
            status_code: int = response.status
            if status_code != 204:
                logging.info('Unable to [DELETE] /orders')
                logging.info(f'Status Code: {status_code}')
            else:
                logging.info('Successsfully canceled all Orders.')


@rate_limit_decorator
async def post_orders(
    paradigm_maker_access_key: str,
    paradigm_maker_secret_key: str,
    payload: Dict
        ) -> None:
    """
    Paradigm RESToverHTTP endpoint.
    [POST] /orders
    """
    method: str = 'POST'
    path: str = '/v1/fs/orders'

    _payload: Dict = payload.copy()
    _payload.pop('order_id', None)
    __payload: str = json.dumps(_payload)

    headers: Dict = await create_rest_headers(
        paradigm_maker_access_key=paradigm_maker_access_key,
        paradigm_maker_secret_key=paradigm_maker_secret_key,
        method=method,
        path=path,
        body=__payload
        )

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                paradigm_http_url+path,
                headers=headers,
                json=_payload
                    ) as response:
                status_code: int = response.status
                response: Dict = await response.json(content_type=None)
                if status_code == 201:
                    logging.info(f'Order Created: {status_code} | Response: {response}')
                else:
                    logging.info('Unable to [POST] /orders')
                    logging.info(f'Status Code: {status_code}')
                    logging.info(f'Response Text: {response}')
                    logging.info(f'Order Payload: {payload}')
        except aiohttp.ClientConnectorError as e:
            logging.error(f'[POST] /orders ClientConnectorError: {e}')


@rate_limit_decorator
async def post_orders_orderid_replace(
    paradigm_maker_access_key: str,
    paradigm_maker_secret_key: str,
    order_id: str,
    payload: Dict
        ) -> None:
    """
    Paradigm RESToverHTTP endpoint.
    [POST] /orders/{order_id}/replace
    """
    method: str = 'POST'
    path: str = f'/v1/fs/orders/{order_id}/replace'

    _payload: Dict = dict(payload)
    for key in ['side', 'strategy_id']:
        _payload.pop(key)

    _payload['order_id'] = order_id
    __payload: str = json.dumps(_payload)

    headers: Dict = await create_rest_headers(
        paradigm_maker_access_key=paradigm_maker_access_key,
        paradigm_maker_secret_key=paradigm_maker_secret_key,
        method=method,
        path=path,
        body=__payload
        )

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                paradigm_http_url+path,
                headers=headers,
                json=_payload
                    ) as response:
                status_code: int = response.status
                response: Dict = await response.json(content_type=None)
                if status_code == 201:
                    logging.info(f'Order Replaced: {status_code} | Response: {response}')
                else:
                    try:
                        if response['code'] == 4003:
                            pass
                        else:
                            logging.info(f'Unable to [POST] /orders/{order_id}/replace')
                            logging.info(f'Status Code: {status_code}')
                            logging.info(f'Response Text: {response}')
                            logging.info(f'Order Payload: {payload}')
                    except KeyError as e:
                        logging.info(e)
                        logging.info(f'[POST] replace | Status Code: {status_code}')
                        logging.info(response)
                    except TypeError as e:
                        logging.info(e)
                        logging.info(f'[POST] replace | Status Code: {status_code}')
                        logging.info(response)
        except aiohttp.ClientConnectorError as e:
            logging.error(f'[POST] /orders ClientConnectorError: {e}')


# JSON-RPCoverWebsocket Interface
async def send_heartbeat(
    websocket: websockets.WebSocketClientProtocol
        ) -> None:
    """
    Sends a Heartbeat to keep the Paradigm WebSocket connection alive.
    """
    while True:
        await websocket.send(
            json.dumps(
                {
                    "id": 1,
                    "jsonrpc": "2.0",
                    "method": "heartbeat"
                }
            )
            )
        await asyncio.sleep(5)


async def subscribe_strategy_state_notifications(
    websocket: websockets.WebSocketClientProtocol
        ) -> None:
    """
    Subscribe to the `strategy_stage.{venue}.{kind}` WS Channel.
    """
    await websocket.send(
        json.dumps(
            {
                "id": 2,
                "jsonrpc": "2.0",
                "method": "subscribe",
                "params": {
                    "channel": "strategy_state.ALL.ALL"
                    }
            }
            )
        )


async def subscribe_orders_notifications(
    websocket: websockets.WebSocketClientProtocol
        ) -> None:
    """
    Subscribe to the `orders.{venue}.{kind}.{strategy_id}` WS Channel.
    """
    await websocket.send(
        json.dumps(
            {
                "id": 3,
                "jsonrpc": "2.0",
                "method": "subscribe",
                "params": {
                    "channel": "orders.ALL.ALL.ALL"
                    }
            }
            )
        )


async def subscribe_venue_bbo_notification(
    websocket: websockets.WebSocketClientProtocol
        ) -> None:
    """
    Subscribe to the `venue_bbo.{strategy_id}` WS Channel.
    """
    await websocket.send(
        json.dumps(
            {
                "id": 4,
                "jsonrpc": "2.0",
                "method": "subscribe",
                "params": {
                    "channel": "venue_bbo.ALL"
                    }
            }
            )
        )


if __name__ == "__main__":
    # Local Testing
    # TEST
    # os.environ['LOGGING_LEVEL'] = 'INFO'
    # os.environ['PARADIGM_MAKER_ACCOUNT_NAME'] = '<venue-api-key-name-on-paradigm>'
    # os.environ['PARADIGM_MAKER_ACCESS_KEY'] = '<access-key>'
    # os.environ['PARADIGM_MAKER_SECRET_KEY'] = '<secret-key>'
    # os.environ['ORDER_NUMBER_PER_SIDE'] = "5"
    # os.environ['QUOTE_QUANTITY_LOWER_BOUNDARY'] = "10000"
    # os.environ['QUOTE_QUANTITY_HIGHER_BOUNDARY'] = "100000"
    # os.environ['QUOTE_PRICE_LOWER_BOUNDARY'] = "100"
    # os.environ['QUOTE_PRICE_HIGHER_BOUNDARY'] = "250"
    # os.environ['QUOTE_REFRESH_LOWER_BOUNDARY'] = "2"
    # os.environ['QUOTE_REFRESH_HIGHER_BOUNDARY'] = "5"

    # Paradigm Connection URLs
    paradigm_environment = os.getenv('PARADIGM_ENVIRONMENT', 'TEST')
    paradigm_ws_url: str = f'wss://ws.api.fs.{paradigm_environment.lower()}.paradigm.co/v1/fs'
    paradigm_http_url: str = f'https://api.fs.{paradigm_environment.lower()}.paradigm.co'

    # Logging
    logging.basicConfig(
        level=os.environ['LOGGING_LEVEL'],
        format='%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
        )

    try:
        # Start the Event Loop
        asyncio.get_event_loop().run_until_complete(
            main(
                paradigm_ws_url=paradigm_ws_url,
                paradigm_http_url=paradigm_http_url,
                paradigm_maker_account_name=os.environ['PARADIGM_MAKER_ACCOUNT_NAME'],
                paradigm_maker_access_key=os.environ['PARADIGM_MAKER_ACCESS_KEY'],
                paradigm_maker_secret_key=os.environ['PARADIGM_MAKER_SECRET_KEY'],
                order_number_per_side=os.environ['ORDER_NUMBER_PER_SIDE'],
                quote_quantity_lower_boundary=os.environ['QUOTE_QUANTITY_LOWER_BOUNDARY'],
                quote_quantity_higher_boundary=os.environ['QUOTE_QUANTITY_HIGHER_BOUNDARY'],
                quote_price_tick_diff_lower_boundary=os.environ['QUOTE_PRICE_LOWER_BOUNDARY'],
                quote_price_tick_diff_higher_boundary=os.environ['QUOTE_PRICE_HIGHER_BOUNDARY'],
                quote_refresh_lower_boundary=os.environ['QUOTE_REFRESH_LOWER_BOUNDARY'],
                quote_refresh_higher_boundary=os.environ['QUOTE_REFRESH_HIGHER_BOUNDARY']
                )
            )
        asyncio.get_event_loop().run_forever()
    except Exception as e:
        logging.info('Local Main Error')
        logging.info(e)
        traceback.print_exc()
