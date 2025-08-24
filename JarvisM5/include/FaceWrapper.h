#pragma once
#include "Face.h"          // из lib/esp32-eyes

class FaceWrapper {
public:
  FaceWrapper(int w, int h, int eyeSz);
  void update()              { face_->Update(); }
  FaceExpression& expr()     { return face_->Expression; }

private:
  Face* face_;
};
