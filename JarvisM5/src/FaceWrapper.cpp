#include "FaceWrapper.h"

FaceWrapper::FaceWrapper(int w, int h, int eyeSz)
{
  face_ = new Face(w, h, eyeSz);
  face_->Expression.GoTo_Normal();
  face_->RandomBehavior = false;
  //face_->Behavior.Timer.SetIntervalMillis(30000);
  face_->RandomBlink    = true;
  face_->Blink.Timer.SetIntervalMillis(5000);
  face_->RandomLook     = true;
}
