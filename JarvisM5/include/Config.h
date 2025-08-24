#pragma once
#include <M5Unified.h>

// ───── Wi-Fi / WebSocket (не нужны при BT) ─────
constexpr char      WIFI_SSID[]     = "Redmi_A33D";
constexpr char      WIFI_PASSWORD[] = "88882222";
constexpr char      WS_HOST[]       = "192.168.31.125";
constexpr uint16_t  WS_PORT         = 8765;
constexpr char      WS_PATH[]       = "/";

// ── Переключатели транспорта ────────────────────────────────────────────────
constexpr bool USE_BT_CLASSIC = false;   // ← выключаем BT, если идём «по проводу»
constexpr bool USE_BLE        = false;
constexpr bool USE_USB_SERIAL = true;    // ← включаем USB CDC JSON

// ── Имена устройств ─────────────────────────────────────────────────────────
constexpr char  BT_DEVICE_NAME[]  = "M5Stack";
constexpr char  BLE_DEVICE_NAME[] = "M5Stack";

// ───── Привязка к месту (для погоды) ─────
constexpr float     WEATHER_LAT  = 53.3725f;
constexpr float     WEATHER_LON  = 58.9824f;
constexpr long      GMT_OFFSET   = 5 * 3600;
constexpr uint32_t  SYNC_PERIOD  = 2 * 60 * 1000UL;

// ───── Экранные области ─────
struct Region { uint16_t x, y, w, h; };
constexpr Region TIME_R       {   0,  0, 100, 16 };
constexpr Region WEATHER_R    { 100,  0,  80, 16 };
constexpr Region ICONS_R      { 180,  0,  80, 16 };
constexpr Region BATTERY_R    { 260,  0,  60, 16 };
constexpr Region MAIN_R       {   0, 40, 320,200 };
constexpr Region MENU_R       {  20, 60, 280,120 };
constexpr Region LOG_REGION   {  10,200, 300, 40 };

// ───── Цвета / таймауты ─────
constexpr uint16_t COL_BACKGROUND = TFT_BLACK;
constexpr uint16_t COL_FOREGROUND = TFT_WHITE;
constexpr uint16_t COL_HIGHLIGHT  = TFT_RED;
constexpr uint16_t COL_ERR        = TFT_RED;
constexpr unsigned long TEXT_DISPLAY_TIMEOUT = 5000;
constexpr unsigned long LONG_PRESS_MS        = 1000;
constexpr unsigned long MENU_BLINK_INTERVAL  =  500;
const uint32_t DISPLAY_TIMEOUT_MS = 30000;
const uint32_t SLEEP_TIMEOUT_MS   = 60000;

// ── UART (USB CDC) ──────────────────────────────────────────────────────────
constexpr uint32_t SERIAL_BAUD = 921600; // совпадает с python SerialDisplayDriver