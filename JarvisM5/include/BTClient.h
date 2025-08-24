#pragma once
#include <Arduino.h>
#include <BluetoothSerial.h>
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_bt_device.h"
#include "esp_gap_bt_api.h"

#include "Overlay.h"
#include "Emotion.h"
#include "ServoController.h"

class BTClient {
public:
  BTClient(Overlay& ov, Emotion& em, ServoController& sc)
    : ov_(ov), em_(em), servo_(sc) {}

  void begin(const char* deviceName, const char* pin = "1234");
  void loop();
  bool hasRecentInput() const { return (millis() - lastRecv_) < 3000; }

private:
  static const char* errName_(esp_err_t e);
  void handleJson_(const String& line);

private:
  BluetoothSerial bt_;
  Overlay& ov_; Emotion& em_; ServoController& servo_;
  String line_; uint32_t lastRecv_ = 0;
};
