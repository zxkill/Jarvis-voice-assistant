#pragma once

enum class UIMode { Sleep, Boot, Run };

void setUIMode(UIMode m);
UIMode getUIMode();
