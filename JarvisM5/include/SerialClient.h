#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

#include "Overlay.h"
#include "Emotion.h"
#include "ServoController.h"
#include "Logger.h"

class SerialClient {
public:
  SerialClient(Overlay& ov, Emotion& em, ServoController& sc)
    : ov_(ov), em_(em), servo_(sc) {}

  // Инициализация. baud по умолчанию совпадает с Python (см. Config.h → SERIAL_BAUD)
  void begin(uint32_t baud);
  // Внутренний цикл чтения/разбора JSON-строк
  void loop();
  // Нужен EnergyManager-у, чтобы понимать «есть ли жизнь на линии»
  bool hasRecentInput() const { return (millis() - lastRecv_) < 3000; }

  // При желании можно отправлять события на Python (для хэндшейка/пинга)
  void sendEvent(const char* kind, const char* payload);

private:
  void handleJson_(const String& line);

private:
  Overlay& ov_;
  Emotion& em_;
  ServoController& servo_;

  String   line_;               // аккумулятор для построчного чтения
  uint32_t lastRecv_ = 0;       // метка активности
  uint32_t lastHello_ = 0;      // чтобы не спамить hello
};
