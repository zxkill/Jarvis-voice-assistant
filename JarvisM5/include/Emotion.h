#pragma once
#include <map>
#include <functional>
#include "FaceWrapper.h"

class Emotion {
public:
  explicit Emotion(FaceWrapper& f);
  void handle(const char* key);

private:
  FaceWrapper& face_;
  std::map<String, std::function<void()>> map_;
};
