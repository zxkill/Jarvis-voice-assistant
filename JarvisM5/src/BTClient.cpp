#include "BTClient.h"
#include <ArduinoJson.h>

const char* BTClient::errName_(esp_err_t e){
  switch(e){
    case ESP_OK: return "ESP_OK";
    case ESP_ERR_NO_MEM: return "ESP_ERR_NO_MEM";
    case ESP_ERR_INVALID_STATE: return "ESP_ERR_INVALID_STATE";
    case ESP_ERR_INVALID_ARG: return "ESP_ERR_INVALID_ARG";
    default: return "ESP_ERR_xxx";
  }
}

void BTClient::begin(const char* deviceName, const char* pin) {
  if (btStarted()) {
    // При перезапуске Bluetooth выводим сообщение в системный лог, но
    // не засоряем USB Serial.
    Logger::log(LogLevel::INFO, "[BT] btStop()");
    btStop();
    delay(50);
  }

  // Далее все диагностические сообщения печатаем через Logger,
  // чтобы они попадали только на экран.
  Logger::log(LogLevel::INFO, "[BT] mem_release(BLE) → %s", errName_(esp_bt_controller_mem_release(ESP_BT_MODE_BLE)));

  esp_bt_controller_config_t cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
  Logger::log(LogLevel::INFO, "[BT] controller_init → %s", errName_(esp_bt_controller_init(&cfg)));
  Logger::log(LogLevel::INFO, "[BT] controller_enable(CLASSIC) → %s", errName_(esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT)));
  Logger::log(LogLevel::INFO, "[BT] bluedroid_init → %s", errName_(esp_bluedroid_init()));
  Logger::log(LogLevel::INFO, "[BT] bluedroid_enable → %s", errName_(esp_bluedroid_enable()));

  Logger::log(LogLevel::INFO, "[BT] set_device_name('%s') → %s", deviceName, errName_(esp_bt_dev_set_device_name(deviceName)));
  Logger::log(LogLevel::INFO,
              "[BT] set_scan_mode(CONNECTABLE,DISCOVERABLE) → %s",
              errName_(esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE)));

  // PIN + SSP
  if (pin && *pin) {
    esp_bt_pin_type_t pin_type = ESP_BT_PIN_TYPE_FIXED;
    esp_bt_pin_code_t pin_code; memset(pin_code, 0, sizeof(pin_code)); memcpy(pin_code, pin, min<size_t>(4, strlen(pin)));
    Logger::log(LogLevel::INFO, "[BT] gap_set_pin → %s", errName_(esp_bt_gap_set_pin(pin_type, 4, pin_code)));
    bt_.enableSSP();
    bt_.setPin(pin);
  }

  bool ok = bt_.begin(deviceName, false);
  Logger::log(LogLevel::INFO, "[BT] BluetoothSerial.begin('%s') → %s", deviceName, ok ? "OK" : "FAIL");
  const uint8_t* mac = esp_bt_dev_get_address();
  if (mac)
    Logger::log(LogLevel::INFO,
                "[BT] READY. MAC %02X:%02X:%02X:%02X:%02X:%02X | PIN %s",
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5],
                pin && *pin ? pin : "(none)");
}

void BTClient::loop() {
  while (bt_.available()) {
    int c = bt_.read(); if (c < 0) break;
    lastRecv_ = millis();
    char ch = (char)c;
    if (ch == '\n') {
      if (line_.length()) { handleJson_(line_); line_.clear(); }
    } else if (ch != '\r') {
      line_ += ch; if (line_.length() > 1024) line_.clear();
    }
  }
}

void BTClient::handleJson_(const String& s) {
  StaticJsonDocument<256> d; DeserializationError e = deserializeJson(d, s);
  if (e) {
    Logger::log(LogLevel::ERROR, "[BT] JSON error: %s | '%s'", e.c_str(), s.c_str());
    return;
  }
  const char* kind = d["kind"] | "";
  if (!strcmp(kind,"time"))      { const char* t=d["payload"]|""; ov_.setTime(t); }
  else if (!strcmp(kind,"weather")){ const char* t=d["payload"]|""; ov_.setWeather(t); }
  else if (!strcmp(kind,"text")) { const char* t=d["payload"]|""; ov_.setText(t); }
  else if (!strcmp(kind,"emotion")){ const char* t=d["payload"]|""; em_.handle(t); }
  else if (!strcmp(kind,"servo")) { auto p=d["payload"].as<JsonObject>(); servo_.setAngles(p["yaw"]|0.0f, p["pitch"]|0.0f); }
  else {
    Logger::log(LogLevel::WARN, "[BT] Unknown kind '%s'", kind);
  }
}
