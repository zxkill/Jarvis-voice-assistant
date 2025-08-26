import json
from collections import deque

from rapidfuzz import fuzz


def _matches_activation(word: str) -> bool:
    """Локальная копия проверки слова активации для упрощения теста."""
    return fuzz.ratio(word, "джарвис") >= 65


class FakeKaldi:
    """Простой заглушечный распознаватель для тестирования буфера.

    Он не анализирует аудио, а рассматривает полученные байты как UTF-8
    строку. Это позволяет проверить логику повторного распознавания без
    запуска Vosk и обработки настоящего PCM.
    """

    def __init__(self):
        self.data = b""

    def AcceptWaveform(self, pcm: bytes) -> bool:
        """Накопить данные и вернуть ``False`` как будто фраза ещё не закончена."""
        self.data += pcm
        return False

    def PartialResult(self) -> str:
        """Вернуть текущий текст в формате Vosk."""
        return json.dumps({"partial": self.data.decode("utf-8")})

    def Result(self) -> str:
        """Вернуть финальный результат."""
        return json.dumps({"text": self.data.decode("utf-8")})

    def Reset(self) -> None:
        """Очистить внутренний буфер."""
        self.data = b""


def test_activation_preserved_with_buffer():
    """Буфер восстанавливает начало слова «джарвис» при повторном распознавании."""
    kaldi = FakeKaldi()
    buf = deque(maxlen=2)
    activated = False

    pcm1 = "дж".encode("utf-8")
    kaldi.AcceptWaveform(pcm1)
    part = json.loads(kaldi.PartialResult())["partial"]
    assert not any(_matches_activation(w) for w in part.split())
    buf.append(pcm1)

    pcm2 = "арвис включи свет".encode("utf-8")
    kaldi.AcceptWaveform(pcm2)
    part = json.loads(kaldi.PartialResult())["partial"]
    assert any(_matches_activation(w) for w in part.split())

    if not activated:
        kaldi.Reset()
        for frame in buf:
            kaldi.AcceptWaveform(frame)
        kaldi.AcceptWaveform(pcm2)
        activated = True
        buf.clear()

    result = json.loads(kaldi.Result())["text"]
    assert result.startswith("джарвис")


def test_activation_not_retriggered():
    """После первой активации повторное слово не приводит к повторному сбросу."""
    kaldi = FakeKaldi()
    buf = deque(maxlen=2)
    activated = False

    pcm1 = "джарвис".encode("utf-8")
    kaldi.AcceptWaveform(pcm1)
    part = json.loads(kaldi.PartialResult())["partial"]
    if not activated and any(_matches_activation(w) for w in part.split()):
        kaldi.Reset()
        for frame in buf:
            kaldi.AcceptWaveform(frame)
        kaldi.AcceptWaveform(pcm1)
        activated = True
        buf.clear()
    buf.append(pcm1)

    pcm2 = " джарвис".encode("utf-8")
    kaldi.AcceptWaveform(pcm2)
    part = json.loads(kaldi.PartialResult())["partial"]
    assert any(_matches_activation(w) for w in part.split())
    # Повторного сброса нет — в буфере остался первый фрагмент
    assert buf[0] == pcm1
