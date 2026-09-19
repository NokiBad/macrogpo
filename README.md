# macrogpo
macro 

## Fishing Bot GPO

`fishing_macroX.py` é um macro de pesca com interface Tkinter. Ele captura
áreas configuradas da janela do jogo, acompanha o minijogo de pesca e usa OCR
somente para decisões de fruta e contador de isca. A automação não implementa
combate, exploração, duplicação/obtenção indevida de itens, injeção, leitura ou
escrita de memória, bypass/evasão de anti-cheat, webhooks, Discord ou qualquer
fluxo fora da pesca.

### Instalação

Requer Python compatível com o código e as dependências:

```text
pip install mss numpy opencv-python pydirectinput keyboard pytesseract
```

O executável Tesseract-OCR também precisa estar instalado. A configuração
opcional do caminho padrão está no próprio script:
`C:\Program Files\Tesseract-OCR\tesseract.exe`.

### Execução

Execute:

```text
python fishing_macroX.py
```

Configure as áreas, cores, baseline, frutas e posições de isca pela interface.
O fluxo recomendado é:

1. Ajustar as áreas.
2. Calibrar as cores.
3. Capturar o slot normal/vazio.
4. Selecionar as opções e iniciar o macro.

`F8` é a tecla de emergência. O fechamento da janela usa `WM_DELETE_WINDOW`,
para workers, libera o mouse, remove a hotkey e fecha overlays.

## Melhorias de confiabilidade e segurança operacional

### Endurecimento operacional, OCR, threads e configuração

- OCR vazio ou incerto nunca é tratado como fruta nova: a pesca pausa, registra
  o motivo e aguarda revisão.
- O contador de isca diferencia OCR inválido de zero e exige leituras válidas
  consecutivas antes de trocar ou pausar.
- Atualizações de widgets passam pela thread principal; workers e hotkey não
  manipulam Tkinter diretamente.
- Workers têm tratamento explícito de falhas, liberam o mouse e atualizam o
  estado/controles de forma idempotente.
- A configuração usa caminhos relativos ao arquivo, defaults explícitos,
  validação de regiões e salvamento atômico.
- O log tenta usar `fishing_bot.log`; se o arquivo não puder ser aberto, há
  fallback explícito para o console, sem ocultar o erro.

### Janela e área cliente

- `WindowTracker` localiza a janela do Roblox por APIs documentadas do Windows,
  obtendo HWND, título, área cliente, monitor, primeiro plano, minimização e
  tamanho.
- Em plataformas ou ambientes sem as APIs do Windows, existe fallback
  explícito para o monitor capturado pelo `mss`.
- As regiões são convertidas para coordenadas de tela a partir da área cliente,
  e cliques de fallback são bloqueados quando ficam fora dela.

### Resolução, monitor e DPI

- A calibração registra tamanho da área cliente e DPI.
- A origem `left/top` do monitor é preservada, inclusive para monitores com
  coordenadas negativas.
- Configurações antigas baseadas no monitor podem ser migradas para a área
  cliente quando há contexto suficiente; regiões inválidas são rejeitadas.
- Alterações incompatíveis de tamanho, monitor ou DPI bloqueiam o pré-voo e
  exigem recalibração.

### Validação e mudança de layout

Antes de iniciar, o programa verifica se a janela está localizada, em primeiro
plano, não minimizada e compatível com a calibração. Durante os workers, perda
de foco, minimização ou mudança de posição/tamanho/monitor/DPI pausa a pesca e
libera o mouse. O estado permanece pausado até uma ação explícita do usuário.

### Diagnóstico sem interação

O botão **Diagnóstico (sem interação)** captura a área cliente e registra
janela, HWND, foco, minimização, monitor, resolução, DPI, dimensões da captura
e coordenadas das regiões. Esse modo não envia cliques, teclas ou movimentos de
mouse e não altera o estado do jogo.

### Correção do overlay de calibração

O overlay de **Ajustar Areas** foi corrigido para evitar uma falha em que os
logs apareciam, mas os cinco retângulos não eram visíveis. A causa provável era
a combinação do `-transparentcolor` do Tkinter com `stipple` no canvas, agravada
por diferenças entre pixels físicos e unidades Tk em telas com DPI escalado.

A solução usa uma sobreposição semitransparente real (`alpha` 0.28), retângulos
com contorno espesso sem preenchimento pontilhado, `topmost`/`lift`/foco
reforçados e configuração de DPI por monitor antes de criar a interface. O
jogo continua visível por trás, enquanto as áreas e alças permanecem
arrastáveis, redimensionáveis, confirmáveis e canceláveis. As coordenadas são
ordenadas e limitadas ao canvas antes de serem persistidas.

Os testes automatizados cobrem a normalização/clamp da geometria, inclusive
retângulos invertidos, limites do canvas e regiões mínimas. A validação visual
do overlay Tkinter em uma máquina Windows com o Roblox aberto ainda é manual;
este ambiente não executa uma GUI real nem captura uma janela do jogo.

## Limites e operação segura

- A automação depende da janela correta do Roblox estar visível e em primeiro
  plano; não tenta controlar uma janela desconhecida.
- OCR pode ser inconclusivo e requer revisão manual.
- Mudanças de layout, escala do sistema ou monitor exigem nova calibração.
- O usuário deve equipar a vara e preparar o jogo antes de iniciar.
- O uso pode violar os termos de serviço do Roblox; use por sua conta e risco.

## Testes

Os testes de lógica pura não dependem de jogo, GUI ou captura real:

```text
python -m unittest discover -v
python -m py_compile fishing_macroX.py
git diff --check
```

Eles cobrem OCR, contador de isca, configuração, salvamento, coordenadas com
origem negativa, migração de monitor para cliente, limites, DPI, estados,
finalização de workers, fallback de logging, métricas e geometria do overlay.

### Renomeação do programa

O arquivo principal e o módulo de testes usam os nomes `fishing_macroX.py` e
`test_fishing_macroX.py`. O `X` faz parte literalmente do nome solicitado.
O número de versão permanece somente no cabeçalho do arquivo/projeto; títulos
da janela, textos da GUI, nomes de executável e documentação operacional não
exibem mais a versão.
