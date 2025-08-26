#include "SerialClient.h"
#include "UIMode.h"

void SerialClient::begin(uint32_t baud) {
  Serial.begin(baud);
  delay(50);

  // Больше НИЧЕГО в порт! Никаких println/логов — только JSON ниже.
  // Первый "hello" — строго JSON:
  sendEvent("hello", "ready");
  lastHello_ = millis();
  lastRecv_ = millis();  // стартовый таймер ожидания ответа хоста
}

void SerialClient::loop() {
  // Периодический hello на случай reconnection (не чаще раза в 2 сек)
  if (millis() - lastHello_ > 2000) {
    sendEvent("hello", "ping");
    lastHello_ = millis();
  }

  // Если долго нет ответов от хоста — перезапускаемся и ждём новое подключение
  if (millis() - lastRecv_ > 5000) {
    ESP.restart();
  }

  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    lastRecv_ = millis();

    char ch = (char)c;
    if (ch == '\n') {
      if (line_.length()) {
        handleJson_(line_);
        line_.clear();
      }
    } else if (ch != '\r') {
      line_ += ch;
      if (line_.length() > 1024) {
        Logger::log(LogLevel::WARN, "[SER] line overflow, dropping");
        line_.clear();
      }
    }
  }
}

void SerialClient::handleJson_(const String& s) {
  StaticJsonDocument<256> d;
  DeserializationError err = deserializeJson(d, s);
  if (err) {
    Logger::log(LogLevel::ERROR, "[SER] JSON error: %s | '%s'", err.c_str(), s.c_str());
    return;
  }

  const char* kind = d["kind"] | "";
  Logger::log(LogLevel::DEBUG, "[SER] kind='%s'", kind);

  if (!strcmp(kind, "time")) {
    const char* t = d["payload"] | "";
    ov_.setTime(t);
  }
  else if (!strcmp(kind, "weather")) {
    const char* t = d["payload"] | "";
    ov_.setWeather(t);
  }
  else if (!strcmp(kind, "text")) {
    const char* t = d["payload"] | "";
    ov_.setText(t);
  }
  else if (!strcmp(kind, "emotion")) {
    const char* t = d["payload"] | "";
    em_.handle(t);
  }
  else if (!strcmp(kind, "mode")) {
    const char* t = d["payload"] | "";
    if (!strcmp(t, "boot"))
      setUIMode(UIMode::Boot);
    else if (!strcmp(t, "run"))
      setUIMode(UIMode::Run);
    else
      setUIMode(UIMode::Sleep);
  }
  else if (strcmp(kind, "track") == 0) {
    const JsonObject p = d["payload"].as<JsonObject>();
    float dx = p["dx_px"] | 0.0f;
    float dy = p["dy_px"] | 0.0f;
    uint32_t dt = p["dt_ms"] | 0;
    servo_.updateFromError(dx, dy, dt);
    Logger::log(LogLevel::DEBUG, "[SER] track: dx=%.1f dy=%.1f dt=%u", dx, dy, (unsigned)dt);
  }
  else if (!strcmp(kind, "log")) {
    // Управление выводом логов в USB Serial: "on"/"off"
    const char* t = d["payload"] | "off";
    bool en = !strcmp(t, "on");
    Logger::enableSerialLogging(en);
    Logger::log(LogLevel::INFO, en ? "[SER] serial logging ON" : "[SER] serial logging OFF");
  }
  else if (!strcmp(kind, "hello")) {
    // keep-alive от хоста, ничего делать не нужно
  }
  else {
    Logger::log(LogLevel::WARN, "[SER] Unknown kind '%s'", kind);
  }
}

void SerialClient::sendEvent(const char* kind, const char* payload) {
  StaticJsonDocument<128> d;
  d["kind"]    = kind ? kind : "";
  d["payload"] = payload ? payload : "";

  // Отправляем одной «пакетной» записью, потом '\n'
  char buf[160];
  size_t n = serializeJson(d, buf, sizeof(buf));
  Serial.write((const uint8_t*)buf, n);
  Serial.write('\n');
  Serial.flush();  // гарантируем, что строка ушла целиком до следующей
}
