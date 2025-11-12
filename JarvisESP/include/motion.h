#pragma once
#include <Arduino.h>
#include "encoder_dual.h"
#include "orientation.h"
#include "motion_params.h"

// Внимание по конвенции: положительный угол — ПОВОРОТ ВЛЕВО.
// Левое колесо — то, что физически слева при взгляде вперёд по курсу.

namespace Motion {

// Инициализация PWM/LEDC, привязка пинов, логирование параметров
bool init(const Params& p);

// Мягкое обновление параметров (без переинициализации PWM), когда меняются только коэффициенты
bool update_runtime(const Params& p);

// Высокоуровневые команды движения (блокирующие, с простым окончанием по одометрии)
void forward_m(float meters, int duty);
void backward_m(float meters, int duty);
void rotate_deg_enc(float angle_deg, int duty); // +влево, −вправо по энкодерам

// Низкоуровневое управление (моментальное)
void left_forward (int duty);
void left_backward(int duty);
void left_coast  ();
void right_forward (int duty);
void right_backward(int duty);
void right_coast  ();

// Общий стоп
void stop_all();

/**
 * \brief Запрашивает досрочное завершение текущего движения.
 *
 * Функция используется потоками высокого уровня (например, веб-интерфейсом)
 * для инициирования мягкой отмены длительной манёвра. После установки флага
 * текущие блокирующие операции движения периодически проверяют его и
 * завершают цикл, аккуратно затормаживая двигатели.
 */
void request_abort();

/**
 * \brief Сбрасывает флаг отмены, когда движение завершено и робот остановлен.
 */
void clear_abort_request();

/**
 * \brief Проверяет, запрошена ли отмена активного манёвра.
 */
bool is_abort_requested();

// Текущие параметры/диагностика
void get_stats(long& dt_left, long& dt_right, long& peek_left, long& peek_right);
const Params& params();

// Работа с гироскопом/ориентацией
// Настроить адрес MPU и включить/выключить коррекцию в рантайме.
void configure_gyro(uint8_t addr, bool enable);
// Принудительно задать текущий курс (например, после ручного выравнивания).
void reset_heading(float yaw_deg = 0.0f);
// Вызов периодического обновления гироскопа, возвращает true при удачном чтении.
bool update_gyro();
// Получить оценённый курс и угловую скорость (используется в логах и логике).
float current_heading_deg();
float current_heading_deg_wrapped();
float current_turn_rate_dps();
float current_gyro_bias_dps();
// Проверка на «застревание» по гироданным.
bool check_stuck(uint32_t now_ms);

} // namespace Motion
