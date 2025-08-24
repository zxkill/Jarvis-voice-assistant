import asyncio
import json
import queue
import threading
import traceback
import websockets

from dataclasses import dataclass
from typing import Any

from display import DisplayDriver, DisplayItem
from core.logging_json import configure_logging

log = configure_logging("display.ws")


@dataclass
class DisplayFrame:
    """Небольшая структура для передачи данных из draw() в отправщик."""
    kind: str      # тип кадра (text, emotion, ...)
    payload: Any   # произвольные данные

class WebsocketDisplayDriver(DisplayDriver):
    """
    WebSocket-драйвер, который:
      * хранит последнее состояние по каждому `kind`;
      * пересылает его новым клиентам после подключения;
      * буферизует кадры в очереди с защитой от переполнения.
    """

    def __init__(self, host="0.0.0.0", port=8765):
        self.host = host
        self.port = port
        self.clients = set()
        # Храним последнее DisplayItem по его kind, чтобы при переподключении
        # сразу восстановить состояние дисплея.
        self._last_items: dict[str, DisplayItem] = {}
        # Общая очередь отправки кадров. maxsize ограничивает рост
        # при всплесках сообщений.
        self._queue: queue.Queue[DisplayFrame] = queue.Queue(maxsize=5)
        self._loop: asyncio.AbstractEventLoop | None = None

        log.info("[INIT] Starting WS server thread")
        self._thread = threading.Thread(
            target=self._run_server_loop,
            name="WS-Server-Thread",
            daemon=True
        )
        self._thread.start()

    def _run_server_loop(self):
        try:
            asyncio.run(self._server_main())
        except Exception as e:
            log.error("[SRV][ERROR] asyncio.run failure: %s", e)
            log.error(traceback.format_exc())

    async def _server_main(self):
        """
        Поднимаем WebSocket-сервер и ждём навсегда.
        """
        # С 12-й версии websockets на Windows есть баг с двойным write_eof().
        # Поэтому принудительно понижаем до 11.0 **или** ловим исключение ниже.
        import websockets
        log.info("[INFO] websockets %s", websockets.__version__)
        async with websockets.serve(
                self._handler,
                self.host, self.port,
                subprotocols=["arduino"],
                # Большие таймауты помогают понять, кто первый закрыл соединение
                ping_interval=4,
                ping_timeout=2,
        ) as server:
            self._loop = asyncio.get_running_loop()
            self._loop.create_task(self._queue_sender())
            log.info("[SRV] WebSocket server started on %s:%s", self.host, self.port)
            await asyncio.Future()

    async def _handler(self, ws: websockets.WebSocketServerProtocol):
        # Новый клиент подключился
        log.info("[WS] Client connected: %s", ws.remote_address)
        self.clients.add(ws)

        # При подключении сразу отправляем все сохранённые кадры,
        # чтобы клиент увидел актуальное состояние.
        for item in self._last_items.values():
            try:
                await ws.send(json.dumps({
                    "kind": item.kind,
                    "payload": item.payload
                }))
                log.debug("[WS] sent cached %s to %s", item.kind, ws.remote_address)
            except Exception:
                log.exception("[WS] failed to send cached %s", item.kind)

        try:
            await ws.wait_closed()
        finally:
            self.clients.remove(ws)
            log.info("[WS] Client disconnected: %s", ws.remote_address)

    async def _queue_sender(self) -> None:
        """Фоновая корутина, пересылающая кадры всем подключённым клиентам."""
        while True:
            # Получаем кадр из очереди (в отдельном потоке, чтобы не блокировать loop)
            frame = await asyncio.to_thread(self._queue.get)
            msg = {"kind": frame.kind, "payload": frame.payload}
            data = json.dumps(msg)
            log.info("[WS→CLIENT] %s", data)
            if not self.clients:
                log.debug("[DRAW] No WS clients — skipping send")
                continue
            await asyncio.gather(*(ws.send(data) for ws in self.clients),
                                  return_exceptions=True)

    def draw(self, item: DisplayItem) -> None:
        """Добавить элемент в очередь отправки и обновить кеш состояния."""
        # Сохраняем состояние; payload=None → удалить из кеша
        if item.payload is None:
            self._last_items.pop(item.kind, None)
        else:
            self._last_items[item.kind] = item

        frame = DisplayFrame(item.kind, item.payload)
        # Для эмоций применяем политику drop_new: если очередь заполнена,
        # просто игнорируем новое выражение, чтобы отобразилась предыдущая.
        if item.kind == "emotion" and self._queue.full():
            log.warning("display.drop {kind:%s, q_len:%d}", item.kind, self._queue.qsize())
            return
        # Для остальных кадров применяем drop_oldest: удаляем самый старый
        # элемент перед добавлением нового.
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            log.warning("display.drop {kind:%s, q_len:%d}", item.kind, self._queue.qsize())
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            log.warning("display.drop {kind:%s, q_len:%d}", item.kind, self._queue.qsize())

    def forget(self, kind: str) -> None:
        """Удалить сохранённое состояние по заданному виду."""
        self._last_items.pop(kind, None)

    def process_events(self) -> None:
        # не нужно в основном loop
        return

    def close(self):
        # При необходимости можно добавить корректную остановку сервера
        pass
