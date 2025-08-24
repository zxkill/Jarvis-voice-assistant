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
  if (btStarted()) { Serial.println("[BT] btStop()"); btStop(); delay(50); }

  Serial.printf("[BT] mem_release(BLE) → %s\n", errName_(esp_bt_controller_mem_release(ESP_BT_MODE_BLE)));

  esp_bt_controller_config_t cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
  Serial.printf("[BT] controller_init → %s\n", errName_(esp_bt_controller_init(&cfg)));
  Serial.printf("[BT] controller_enable(CLASSIC) → %s\n", errName_(esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT)));
  Serial.printf("[BT] bluedroid_init → %s\n", errName_(esp_bluedroid_init()));
  Serial.printf("[BT] bluedroid_enable → %s\n", errName_(esp_bluedroid_enable()));

  Serial.printf("[BT] set_device_name('%s') → %s\n", deviceName, errName_(esp_bt_dev_set_device_name(deviceName)));
  Serial.printf("[BT] set_scan_mode(CONNECTABLE,DISCOVERABLE) → %s\n",
                errName_(esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE)));

  // PIN + SSP
  if (pin && *pin) {
    esp_bt_pin_type_t pin_type = ESP_BT_PIN_TYPE_FIXED;
    esp_bt_pin_code_t pin_code; memset(pin_code, 0, sizeof(pin_code)); memcpy(pin_code, pin, min<size_t>(4, strlen(pin)));
    Serial.printf("[BT] gap_set_pin → %s\n", errName_(esp_bt_gap_set_pin(pin_type, 4, pin_code)));
    bt_.enableSSP();
    bt_.setPin(pin);
  }

  bool ok = bt_.begin(deviceName, false);
  Serial.printf("[BT] BluetoothSerial.begin('%s') → %s\n", deviceName, ok ? "OK" : "FAIL");
  const uint8_t* mac = esp_bt_dev_get_address();
  if (mac) Serial.printf("[BT] READY. MAC %02X:%02X:%02X:%02X:%02X:%02X | PIN %s\n",
                         mac[0],mac[1],mac[2],mac[3],mac[4],mac[5], pin && *pin ? pin : "(none)");
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
  if (e) { Serial.printf("[BT] JSON error: %s | '%s'\n", e.c_str(), s.c_str()); return; }
  const char* kind = d["kind"] | "";
  if (!strcmp(kind,"time"))      { const char* t=d["payload"]|""; ov_.setTime(t); }
  else if (!strcmp(kind,"weather")){ const char* t=d["payload"]|""; ov_.setWeather(t); }
  else if (!strcmp(kind,"text")) { const char* t=d["payload"]|""; ov_.setText(t); }
  else if (!strcmp(kind,"emotion")){ const char* t=d["payload"]|""; em_.handle(t); }
  else if (!strcmp(kind,"servo")) { auto p=d["payload"].as<JsonObject>(); servo_.setAngles(p["yaw"]|0.0f, p["pitch"]|0.0f); }
  else { Serial.printf("[BT] Unknown kind '%s'\n", kind); }
}
