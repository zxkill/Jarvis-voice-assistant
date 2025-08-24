#pragma once
#include <Arduino.h>
#include <M5Unified.h>
#include "Logger.h"

class EnergyManager {
public:
    void begin() {
        lastActivity_ = millis();
        M5.Display.setBrightness(20);
        screenDimmed_ = false;
    }

    void update(bool recentActivity) {
        uint32_t now = millis();
        uint32_t idleTime = now - lastActivity_;

        if (recentActivity) {
            lastActivity_ = now;
            if (screenDimmed_) {
                M5.Display.setBrightness(20);
                screenDimmed_ = false;
                Logger::log(LogLevel::INFO, "[ENERGY] Яркость восстановлена");
            }
        }

        if (!screenDimmed_ && idleTime > DISPLAY_TIMEOUT_MS) {
            M5.Display.setBrightness(5);
            screenDimmed_ = true;
            Logger::log(LogLevel::INFO, "[ENERGY] Экран затемнён");
        }

        if (idleTime > SLEEP_TIMEOUT_MS) {
            Logger::log(LogLevel::INFO, "[ENERGY] Light sleep на 5 сек");
            esp_sleep_enable_timer_wakeup(5 * 1000000ULL);
            esp_light_sleep_start();
            Logger::log(LogLevel::INFO, "[ENERGY] Проснулись из light sleep");

            M5.Display.setBrightness(20);
            screenDimmed_ = false;
            lastActivity_ = millis();
        }
    }

private:
    uint32_t lastActivity_ = 0;
    bool screenDimmed_ = false;

    static constexpr uint32_t DISPLAY_TIMEOUT_MS = 30000;
    static constexpr uint32_t SLEEP_TIMEOUT_MS   = 60000;
};
