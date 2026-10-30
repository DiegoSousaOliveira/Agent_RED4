# Agent RED4

Agente que aprende a jogar **Resident Evil 4 Remake** (fase da cabana) a partir da imagem do jogo.

Hoje o projeto tem a parte de **captura de tela** pronta e validada: um módulo que entrega os quadros do
jogo como arrays numpy, escolhido depois de um benchmark com as principais bibliotecas Python de captura.

## Requisitos

- Windows 10 ou 11
- Python 3.14 (versão testada)
- Resident Evil 4 Remake em modo janela (testado com a imagem em 1920x1080)

## Instalação

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install windows-capture numpy opencv-python
```

## Capturar o jogo

```python
import cv2
from capturers import WindowsCaptureStreamCapturer

cv2.setNumThreads(1)  # Evita que o pool ocioso do OpenCV quase dobre o uso de CPU

with WindowsCaptureStreamCapturer() as capturer:
    frame = capturer.grab()  # numpy (1080, 1920, 3), uint8, BGR
    cv2.imwrite("jogo.png", frame)
```

- A sessão de captura fica aberta enquanto o `with` durar. Cada `grab()` devolve o quadro mais recente em
  ~1–2 ms, numa imagem nova que é sua.
- O jogo pode ficar **atrás de outras janelas**, mas **nunca minimizado**: minimizado, o Windows para de
  desenhá-lo e nenhuma API consegue capturar.
- A primeira foto leva ~0,2 s (o Windows prepara a sessão). Por isso a sessão é aberta uma vez só; abrir
  uma por foto deixa cada captura ~150× mais lenta.
- No Windows 10 aparece uma borda amarela em volta do jogo enquanto ele é capturado. É o aviso de captura
  do próprio Windows e não sai na imagem.
- Para gravar dados de treino a 60 FPS sem perder quadros, consuma os quadros dentro do callback
  `on_frame_arrived` (numa fila, junto com `frame.timespan`) em vez de chamar `grab()` a 60 Hz. Dentro do
  callback, copie só com numpy: chamar o OpenCV ali derruba o processo.

## Por que `windows-capture` com a sessão aberta

Resultado do benchmark de 27/10/2026 (Ryzen 5 5600XT, RTX 5060, monitor de 60 Hz), detalhado em
[benchmark/RELATORIO.md](benchmark/RELATORIO.md):

| | windows-capture (stream) | PrintWindow (ctypes) | Melhor captura de tela (bettercam) |
| :--- | ---: | ---: | ---: |
| Vê o jogo com outra janela na frente | sim | sim | não |
| Tempo por foto | 1,3 ms | 16,6 ms | 1,2 ms |
| Quadros novos por segundo | 60,2 | 60,0 | 60,2 |
| CPU a 30 FPS | 21,4% | 17,9% | 6,6% |
| CPU a 60 FPS (gravação) | 31,2% | 39,6% | não medido |

As capturas de tela (`mss`, `dxcam`, `bettercam`, `Pillow`, `pyautogui`, `pywin32`) fotografam o monitor:
qualquer janela por cima do jogo entra na imagem. Entre as que capturam a própria janela, a
`windows-capture` não trava o agente (o `PrintWindow` fica ~16 ms parado a cada foto) e gasta menos CPU
gravando a 60 FPS.

## Benchmark

O código do benchmark fica em [benchmark/](benchmark/). Com o jogo aberto (pode estar atrás de outras
janelas):

```powershell
.venv\Scripts\python.exe -m pip install -r benchmark\requirements.txt
.venv\Scripts\python.exe -m benchmark.run
```

Ele mede 10 configurações (7 bibliotecas e o `PrintWindow` via `ctypes`), cada uma em um processo
separado, e gera de novo o
[relatório](benchmark/RELATORIO.md) com gráficos, fotos e o código de cada opção.

## Teclas do jogo

Atribuições de teclas configuradas no jogo ([imgs/conf_teclas_no_jogo.png](imgs/conf_teclas_no_jogo.png)):

| Ação | Tecla |
| :--- | :--- |
| Andar para frente / esquerda / trás | W / A / S |
| Mirar arma, atacar | mouse |
| Empunhar faca / aparar | Espaço |
| Examinar / pegar item, bater | F |
| Recarregar | R |
| Instalar / remover minas | G |
| +/- zoom (mira) | Alt |

## Estrutura

```text
Agent_RED4/
├── capturers.py          # Captura do jogo (windows-capture, sessão contínua)
├── main.py               # Ponto de entrada do agente (vazio por enquanto)
├── imgs/                 # Imagens de referência do jogo
└── benchmark/            # Comparação das bibliotecas de captura
    ├── RELATORIO.md      # Relatório com dados, gráficos e código
    ├── run.py            # Roda o benchmark completo
    ├── worker.py         # Mede uma biblioteca em um processo isolado
    ├── capturers.py      # Um adaptador por biblioteca
    ├── winapi.py         # Chamadas ao Windows via ctypes
    ├── report.py         # Gera gráficos e relatório
    └── results/          # Dados brutos, gráficos e fotos
```
