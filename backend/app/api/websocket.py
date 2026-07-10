import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws/scanner")
async def scanner_socket(websocket: WebSocket) -> None:
    access = websocket.app.state.access
    token = websocket.query_params.get("token")
    session = (
        access.check_session(token, {"user"})
        or access.check_session(token, {"admin"})
        or access.check_session(websocket.cookies.get("alpha_access_token"), {"user"})
        or access.check_session(websocket.cookies.get("alpha_admin_token"), {"admin"})
    )
    if not session:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    websocket.app.state.websocket_count = int(getattr(websocket.app.state, "websocket_count", 0)) + 1

    service = websocket.app.state.scanner
    rankings_queue = service.subscribe_rankings()
    alerts_queue = service.subscribe_alerts()
    perp_rankings_queue = service.subscribe_perp_rankings()
    perp_alerts_queue = service.subscribe_perp_alerts()
    last_rankings_sent = 0.0
    last_perp_rankings_sent = 0.0

    try:
        while True:
            tasks = {
                asyncio.create_task(rankings_queue.get()): "rankings",
                asyncio.create_task(alerts_queue.get()): "alert",
                asyncio.create_task(perp_rankings_queue.get()): "perp_rankings",
                asyncio.create_task(perp_alerts_queue.get()): "perp_alert",
            }
            done, pending = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            for task in done:
                kind = tasks[task]
                payload = task.result()
                if kind == "rankings":
                    now = asyncio.get_running_loop().time()
                    if now - last_rankings_sent < 2.0:
                        continue
                    last_rankings_sent = now
                    await websocket.send_json({"type": "rankings", "data": [item.model_dump() for item in payload[:50]]})
                elif kind == "perp_rankings":
                    now = asyncio.get_running_loop().time()
                    if now - last_perp_rankings_sent < 2.0:
                        continue
                    last_perp_rankings_sent = now
                    await websocket.send_json({"type": "perp_rankings", "data": [item.model_dump() for item in payload[:50]]})
                elif kind == "alert":
                    await websocket.send_json({"type": "alert", "data": payload.model_dump()})
                elif kind == "perp_alert":
                    await websocket.send_json({"type": "perp_alert", "data": payload.model_dump()})
    except WebSocketDisconnect:
        pass
    finally:
        websocket.app.state.websocket_count = max(0, int(getattr(websocket.app.state, "websocket_count", 0)) - 1)
        service.unsubscribe_rankings(rankings_queue)
        service.unsubscribe_alerts(alerts_queue)
        service.unsubscribe_perp_rankings(perp_rankings_queue)
        service.unsubscribe_perp_alerts(perp_alerts_queue)
