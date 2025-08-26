#include "Emotion.h"

Emotion::Emotion(FaceWrapper& f) : face_(f)
{
  auto add = [this](const char* k, const std::function<void()>& fn) {
    map_.emplace(String(k), fn);
  };

  add("Normal",      [this]{ face_.expr().GoTo_Normal();       });
  add("Angry",       [this]{ face_.expr().GoTo_Angry();        });
  add("Glee",        [this]{ face_.expr().GoTo_Glee();         });
  add("Happy",       [this]{ face_.expr().GoTo_Happy();        });
  add("Sad",         [this]{ face_.expr().GoTo_Sad();          });
  add("Worried",     [this]{ face_.expr().GoTo_Worried();      });
  add("Focused",     [this]{ face_.expr().GoTo_Focused();      });
  add("Annoyed",     [this]{ face_.expr().GoTo_Annoyed();      });
  add("Surprised",   [this]{ face_.expr().GoTo_Surprised();    });
  add("Skeptic",     [this]{ face_.expr().GoTo_Skeptic();      });
  add("Frustrated",  [this]{ face_.expr().GoTo_Frustrated();   });
  add("Unimpressed", [this]{ face_.expr().GoTo_Unimpressed();  });
  add("Sleepy",      [this]{ face_.expr().GoTo_Sleepy();       });
  add("Tired",       [this]{ face_.expr().GoTo_Tired();        });  // новая эмоция
  add("Suspicious",  [this]{ face_.expr().GoTo_Suspicious();   });
  add("Squint",      [this]{ face_.expr().GoTo_Squint();       });
  add("Furious",     [this]{ face_.expr().GoTo_Furious();      });
  add("Scared",      [this]{ face_.expr().GoTo_Scared();       });
  add("Awe",         [this]{ face_.expr().GoTo_Awe();          });
}

void Emotion::handle(const char* key)
{
  auto it = map_.find(String(key));
  if (it != map_.end()) it->second();
}
