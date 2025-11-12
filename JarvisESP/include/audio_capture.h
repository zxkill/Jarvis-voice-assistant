#pragma once

#include <stdint.h>
#include <vector>

#include "audio_localization.h"

namespace Audio {

/**
 * \brief Конфигурация аппаратного ввода аудио.
 */
struct Config {
  int pinBclk = 18;                ///< Пин тактовой линии I2S1 BCLK (SCK) — перенесён с GPIO26 ради освобождения ЦАПа.
  int pinWs = 5;                   ///< Пин выборки канала (LRCLK/WS) для I2S1 — перенесён с GPIO27 для развязки с блоком аудиовывода.
  int pinData = 19;                ///< Пин данных микрофона (SD) на I2S1 — перенесён с GPIO25, чтобы DAC1 работал без конфликтов.
  uint32_t sampleRate = 16000;     ///< Частота дискретизации, Гц.
  size_t frameSamples = 512;       ///< Количество сэмплов на канал в одном кадре.
  float microphoneSpacingMeters = 0.15f; ///< Расстояние между микрофонами, м (по умолчанию 15 см).
  bool enableLocalization = false; ///< Выполнять ли расчёт направления на самом роботе (по умолчанию доверяем серверу).
};

/**
 * \brief Данные о последнем захваченном аудиокадре.
 */
struct PcmChunk {
  std::vector<int16_t> interleaved; ///< Интерливированные семплы L,R,L,R...
  uint32_t sampleRate = 0;          ///< Частота дискретизации кадра, Гц.
  uint8_t channels = 2;             ///< Количество каналов (для совместимости).
  uint64_t timestampUs = 0;         ///< Метка времени захвата, мкс.
};

/**
 * \brief Подробная диагностическая информация для телеметрии.
 */
struct Diagnostics {
  bool localizationEnabled = false; ///< Активна ли локальная оценка направления.
  float directionDeg = 0.0f;        ///< Угол на источник звука, градусы (валиден, если localizationEnabled == true).
  float confidence = 0.0f;          ///< Уверенность алгоритма, [0;1] (валидна только при активной локализации).
  float rmsLeft = 0.0f;             ///< RMS левого канала (нормировано).
  float rmsRight = 0.0f;            ///< RMS правого канала (нормировано).
  uint32_t sampleRate = 0;          ///< Частота дискретизации, Гц.
  uint32_t frameSamples = 0;        ///< Размер кадра на канал.
  uint32_t framesCaptured = 0;      ///< Сколько кадров успешно получено.
  uint64_t lastFrameTimestampUs = 0;///< Метка времени последнего кадра.
  bool streamHasChunk = false;      ///< Готов ли кадр для передачи на сервер.
  float microphoneSpacingMeters = 0.0f; ///< Храним расстояние между микрофонами для телеметрии и сервера.
};

bool init(const Config& cfg);
void shutdown();
void poll();
Diagnostics latest_diagnostics();
bool pop_chunk(PcmChunk& out);

} // namespace Audio
