#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace XiaoZhi {

/**
 * \brief Настройки приветственного сообщения и аудиопотока в формате XiaoZhi.
 *
 * По умолчанию копируем поведение открытого проекта xiaozhi-esp32: версия 3,
 * моно 44.1 кГц, длительность кадра 60 мс и формат opus. Мы явно храним формат в
 * строке, чтобы можно было переключиться на pcm16 для отладки без сервера Opus.
 */
struct HelloConfig {
  uint16_t version = 3;          ///< Версия бинарного протокола (2 или 3).
  std::string format = "opus";   ///< Кодек аудио на линии (обычно opus, для отладки можно pcm16).
  uint32_t sampleRate = 44100;   ///< Частота дискретизации, Гц.
  uint16_t channels = 1;         ///< Количество каналов (xiaozhi использует моно).
  uint16_t frameDurationMs = 60; ///< Длительность кадра, мс.
};

/**
 * \brief Представление бинарного кадра XiaoZhi после разборки заголовка.
 */
struct FrameView {
  uint8_t type = 0;                   ///< 0 — аудио, 1 — JSON (текстовая нагрузка).
  std::vector<uint8_t> payload;       ///< Полезная нагрузка без сетевого заголовка.
};

/// Формирует JSON-приветствие по спецификации XiaoZhi (type=hello, audio_params...).
std::string build_hello_json(const HelloConfig& cfg);

/// Упаковывает уже подготовленную аудионагрузку (Opus/PCM) в бинарный протокол версии 2 или 3.
std::vector<uint8_t> build_audio_frame(const HelloConfig& cfg,
                                      const std::vector<uint8_t>& payloadBytes,
                                      uint32_t timestampMs);

/// Пытается разобрать бинарный кадр XiaoZhi, возвращает false при ошибке формата.
bool parse_frame(const uint8_t* data, size_t length, uint16_t version, FrameView& out, std::string& error);

} // namespace XiaoZhi

