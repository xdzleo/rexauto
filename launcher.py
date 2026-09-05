#!/usr/bin/env python3
"""launcher.py -- o launcher grafico que acompanha cada port gerado.

Por que existe: o exe do port nao tem tela de opcoes. Resolucao, monitor, tela
cheia e limite de quadros sao cvars do runtime, e o unico jeito de escolhe-los e
por variavel de ambiente REX_* antes de subir o processo (cvar.cpp:
FlagNameToEnvVar). Sem um launcher isso vira "edite o .cmd na mao".

Por que PowerShell/WinForms: e a unica interface grafica garantida em qualquer
Windows sem instalar nada. Um .exe proprio exigiria compilar mais um alvo, e um
HTML exigiria navegador.
"""
import os

PS1 = r"""# Launcher gerado pelo rexauto para __GAME_TITLE__.
# Escolhe resolucao e algumas opcoes de video, grava a escolha ao lado do exe e
# entrega tudo como variaveis de ambiente REX_* -- que e como o runtime le seus
# cvars (cvar.cpp: FlagNameToEnvVar poe "REX_" na frente e sobe para maiuscula).
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$dir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$exe  = Join-Path $dir '__EXE_NAME__'
$root = '__GAME_ROOT__'
$gpu  = '__GPU_PLUGIN__'
$cfgPath = Join-Path $dir 'launcher.json'

if (-not (Test-Path $exe)) {
  [void][System.Windows.Forms.MessageBox]::Show(("Nao achei o executavel:" + [Environment]::NewLine + $exe), 'rexauto', 'OK', 'Error')
  exit 1
}

# O modo de video do guest vai de 640..4095 em cada eixo (window.cpp:
# video_mode_width .range(640,0x0FFF)). Fora disso o runtime rejeita, entao nem
# oferecemos -- inclusive as ultrawide de 5120 de largura.
$MINW = 640; $MAXW = 4095; $MINH = 480; $MAXH = 4095
function Fits($w, $h) { return ($w -ge $MINW -and $w -le $MAXW -and $h -ge $MINH -and $h -le $MAXH) }

$presets = @(
  @{ n = '1280 x 720   (720p)';  w = 1280; h = 720 },
  @{ n = '1600 x 900';           w = 1600; h = 900 },
  @{ n = '1920 x 1080  (1080p)'; w = 1920; h = 1080 },
  @{ n = '2560 x 1440  (1440p)'; w = 2560; h = 1440 },
  @{ n = '3200 x 1800';          w = 3200; h = 1800 },
  @{ n = '3840 x 2160  (4K)';    w = 3840; h = 2160 }
)

# A resolucao nativa de cada monitor primeiro, quando couber no limite do guest.
$native = @()
foreach ($s in [System.Windows.Forms.Screen]::AllScreens) {
  $w = $s.Bounds.Width; $h = $s.Bounds.Height
  if ((Fits $w $h) -and -not ($native | Where-Object { $_.w -eq $w -and $_.h -eq $h })) {
    $native += @{ n = ("$w x $h   (monitor)"); w = $w; h = $h }
  }
}
$modes = @($native)
foreach ($p in $presets) {
  if ((Fits $p.w $p.h) -and -not ($modes | Where-Object { $_.w -eq $p.w -and $_.h -eq $p.h })) { $modes += $p }
}

$cfg = @{ w = 1280; h = 720; full = $true; monitor = 0; vsync = $true; tolerant = $true;
          scale = 1; aa = 'none'; aniso = 3 }
if (Test-Path $cfgPath) {
  try {
    $j = Get-Content $cfgPath -Raw | ConvertFrom-Json
    foreach ($k in @('w', 'h', 'full', 'monitor', 'vsync', 'tolerant', 'scale', 'aa', 'aniso')) {
      if ($null -ne $j.$k) { $cfg[$k] = $j.$k }
    }
  } catch { }
}

$f = New-Object System.Windows.Forms.Form
$f.Text = '__GAME_TITLE__'
$f.ClientSize = New-Object System.Drawing.Size(454, 476)
$f.StartPosition = 'CenterScreen'
$f.FormBorderStyle = 'FixedDialog'
$f.MaximizeBox = $false
$f.MinimizeBox = $false
$f.BackColor = [System.Drawing.Color]::FromArgb(16, 19, 22)
$f.ForeColor = [System.Drawing.Color]::FromArgb(226, 232, 226)
$f.Font = New-Object System.Drawing.Font('Segoe UI', 9)

function Add-Cap($text, $x, $y, $w = 300) {
  $l = New-Object System.Windows.Forms.Label
  $l.Text = $text
  $l.Location = New-Object System.Drawing.Point($x, $y)
  $l.Size = New-Object System.Drawing.Size($w, 16)
  $l.ForeColor = [System.Drawing.Color]::FromArgb(140, 150, 140)
  $f.Controls.Add($l)
}

$hdr = New-Object System.Windows.Forms.Label
$hdr.Text = '__GAME_TITLE__'
$hdr.Location = New-Object System.Drawing.Point(22, 16)
$hdr.Size = New-Object System.Drawing.Size(410, 28)
$hdr.Font = New-Object System.Drawing.Font('Segoe UI', 13, [System.Drawing.FontStyle]::Bold)
$hdr.ForeColor = [System.Drawing.Color]::FromArgb(155, 240, 11)
$f.Controls.Add($hdr)

Add-Cap 'RESOLUCAO' 22 56
$cbRes = New-Object System.Windows.Forms.ComboBox
$cbRes.Location = New-Object System.Drawing.Point(22, 74)
$cbRes.Size = New-Object System.Drawing.Size(410, 24)
$cbRes.DropDownStyle = 'DropDownList'
$cbRes.BackColor = [System.Drawing.Color]::FromArgb(26, 30, 34)
$cbRes.ForeColor = $f.ForeColor
foreach ($m in $modes) { [void]$cbRes.Items.Add($m.n) }
$sel = 0
for ($i = 0; $i -lt $modes.Count; $i++) {
  if ($modes[$i].w -eq $cfg.w -and $modes[$i].h -eq $cfg.h) { $sel = $i }
}
$cbRes.SelectedIndex = $sel
$f.Controls.Add($cbRes)

Add-Cap 'MONITOR' 22 112
$cbMon = New-Object System.Windows.Forms.ComboBox
$cbMon.Location = New-Object System.Drawing.Point(22, 130)
$cbMon.Size = New-Object System.Drawing.Size(410, 24)
$cbMon.DropDownStyle = 'DropDownList'
$cbMon.BackColor = [System.Drawing.Color]::FromArgb(26, 30, 34)
$cbMon.ForeColor = $f.ForeColor
[void]$cbMon.Items.Add('Padrao do sistema')
$mi = 1
foreach ($s in [System.Windows.Forms.Screen]::AllScreens) {
  [void]$cbMon.Items.Add(("Monitor {0}   ({1} x {2})" -f $mi, $s.Bounds.Width, $s.Bounds.Height))
  $mi++
}
$cbMon.SelectedIndex = [Math]::Min([int]$cfg.monitor, $cbMon.Items.Count - 1)
$f.Controls.Add($cbMon)

$ckFull = New-Object System.Windows.Forms.CheckBox
$ckFull.Text = 'Tela cheia'
$ckFull.Location = New-Object System.Drawing.Point(22, 170)
$ckFull.Size = New-Object System.Drawing.Size(130, 22)
$ckFull.Checked = [bool]$cfg.full
$f.Controls.Add($ckFull)

$ckVsync = New-Object System.Windows.Forms.CheckBox
$ckVsync.Text = 'Limitar quadros (vsync)'
$ckVsync.Location = New-Object System.Drawing.Point(162, 170)
$ckVsync.Size = New-Object System.Drawing.Size(270, 22)
$ckVsync.Checked = [bool]$cfg.vsync
$f.Controls.Add($ckVsync)

$ckTol = New-Object System.Windows.Forms.CheckBox
$ckTol.Text = 'Modo tolerante (nao morre em funcao nao descoberta)'
$ckTol.Location = New-Object System.Drawing.Point(22, 196)
$ckTol.Size = New-Object System.Drawing.Size(410, 22)
$ckTol.Checked = [bool]$cfg.tolerant
$f.Controls.Add($ckTol)

function New-Combo($x, $y, $w, $items, $sel) {
  $c = New-Object System.Windows.Forms.ComboBox
  $c.Location = New-Object System.Drawing.Point($x, $y)
  $c.Size = New-Object System.Drawing.Size($w, 24)
  $c.DropDownStyle = 'DropDownList'
  $c.BackColor = [System.Drawing.Color]::FromArgb(26, 30, 34)
  $c.ForeColor = $f.ForeColor
  foreach ($i in $items) { [void]$c.Items.Add($i) }
  $c.SelectedIndex = [Math]::Max(0, [Math]::Min($sel, $c.Items.Count - 1))
  $f.Controls.Add($c)
  return $c
}

# Escala interna: multiplica os alvos de render do Xenos (o jogo continua
# achando que desenha em 720p). Custo cresce com o quadrado; exige reiniciar.
$scales = @(1, 2, 3, 4)
Add-Cap 'ESCALA DE RENDERIZACAO' 22 226 200
$cbScale = New-Combo 22 244 200 @(
  '1x   nativo do jogo',
  '2x   720p -> 1440p',
  '3x   720p -> 2160p',
  '4x   720p -> 2880p') ([Math]::Max(0, [array]::IndexOf($scales, [int]$cfg.scale)))

# FXAA e pos-processo no swap; o Xenos so expoe o MSAA que o proprio jogo pediu,
# entao nao ha como forcar mais do que isso.
$aas = @('none', 'fxaa', 'fxaa_extreme')
Add-Cap 'ANTI-ALIASING' 232 226 200
$cbAA = New-Combo 232 244 200 @('Desligado', 'FXAA', 'FXAA extremo') `
  ([Math]::Max(0, [array]::IndexOf($aas, [string]$cfg.aa)))

# So afeta textura com mipmap e filtro linear -- UI em point sampling fica intacta.
$anisos = @(-1, 0, 2, 3, 4, 5)
Add-Cap 'FILTRO ANISOTROPICO' 22 282 200
$cbAniso = New-Combo 22 300 200 @(
  'Nao sobrepor', 'Desligado', '2x', '4x (padrao)', '8x', '16x') `
  ([Math]::Max(0, [array]::IndexOf($anisos, [int]$cfg.aniso)))

$note = New-Object System.Windows.Forms.Label
$note.Text = 'Sem o limite de quadros o jogo renderiza o quanto a maquina der -- centenas de fps ate numa tela de menu. Isso e um transiente grande de energia na GPU. Deixe marcado a menos que va medir desempenho. Escala e anti-aliasing valem a partir do proximo inicio.'
$note.Location = New-Object System.Drawing.Point(22, 338)
$note.Size = New-Object System.Drawing.Size(410, 62)
$note.ForeColor = [System.Drawing.Color]::FromArgb(132, 142, 132)
$f.Controls.Add($note)

$btn = New-Object System.Windows.Forms.Button
$btn.Text = 'JOGAR'
$btn.Location = New-Object System.Drawing.Point(22, 412)
$btn.Size = New-Object System.Drawing.Size(410, 42)
$btn.FlatStyle = 'Flat'
$btn.FlatAppearance.BorderSize = 0
$btn.BackColor = [System.Drawing.Color]::FromArgb(120, 190, 30)
$btn.ForeColor = [System.Drawing.Color]::FromArgb(8, 18, 10)
$btn.Font = New-Object System.Drawing.Font('Segoe UI', 11, [System.Drawing.FontStyle]::Bold)
$f.Controls.Add($btn)
$f.AcceptButton = $btn

$btn.Add_Click({
  $m = $modes[$cbRes.SelectedIndex]
  $cfg.w = $m.w; $cfg.h = $m.h
  $cfg.full = $ckFull.Checked
  $cfg.monitor = $cbMon.SelectedIndex
  $cfg.vsync = $ckVsync.Checked
  $cfg.tolerant = $ckTol.Checked
  $cfg.scale = $scales[$cbScale.SelectedIndex]
  $cfg.aa = $aas[$cbAA.SelectedIndex]
  $cfg.aniso = $anisos[$cbAniso.SelectedIndex]
  try { $cfg | ConvertTo-Json | Set-Content -Path $cfgPath -Encoding utf8 } catch { }

  $env:REX_VIDEO_MODE_WIDTH = [string]$m.w
  $env:REX_VIDEO_MODE_HEIGHT = [string]$m.h
  if ($ckFull.Checked) { $env:REX_FULLSCREEN = 'true' } else { $env:REX_FULLSCREEN = 'false' }
  if ($cbMon.SelectedIndex -gt 0) { $env:REX_MONITOR = [string]$cbMon.SelectedIndex }
  if ($ckVsync.Checked) {
    $env:REX_VSYNC = 'true'
    # Com tearing liberado o vsync nao prende nada e o jogo roda sem teto, entao
    # ele tem de cair junto para "limitar quadros" significar alguma coisa.
    $env:REX_D3D12_ALLOW_VARIABLE_REFRESH_RATE_AND_TEARING = 'false'
  } else {
    $env:REX_VSYNC = 'false'
    $env:REX_D3D12_ALLOW_VARIABLE_REFRESH_RATE_AND_TEARING = 'true'
  }
  if ($ckTol.Checked) { $env:REX_HEAL_DISCOVER = '1' }
  if ($gpu) { $env:REX_GPU_PLUGIN = $gpu }
  # cvars do plugin de GPU: o runtime le cada um de REX_<NOME_MAIUSCULO>.
  $env:REX_RESOLUTION_SCALE = [string]$cfg.scale
  $env:REX_SWAP_POST_EFFECT = [string]$cfg.aa
  $env:REX_ANISOTROPIC_OVERRIDE = [string]$cfg.aniso

  $argv = @()
  if ($root) { $argv += ('--game_data_root=' + $root) }
  Start-Process -FilePath $exe -ArgumentList $argv -WorkingDirectory $dir
  $f.Close()
})

[void]$f.ShowDialog()
"""


CMD = (
    "@echo off\r\n"
    "rem Abre o launcher grafico do port (resolucao, monitor, limite de quadros)."
    "\r\n"
    "rem -STA e obrigatorio: WinForms nao roda no apartment padrao do PowerShell."
    "\r\n"
    "powershell -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden "
    "-File \"%~dp0launcher.ps1\"\r\n")


def write(ctx, gpu_plugin=None, log=None):
    """Grava `Launcher <name>.cmd` + `launcher.ps1` ao lado do exe.

    REXAUTO_NO_LAUNCHER=1 pula. Nao levanta: um port que construiu nao pode
    falhar por causa do launcher.
    """
    if os.environ.get("REXAUTO_NO_LAUNCHER"):
        return None
    try:
        title = ctx.name.replace("_", " ").title()
        ps1 = (PS1.replace("__GAME_TITLE__", title)
                  .replace("__EXE_NAME__", "%s.exe" % ctx.name)
                  .replace("__GAME_ROOT__", (ctx.game or '').replace(chr(39), chr(39) * 2))
                  .replace("__GPU_PLUGIN__", gpu_plugin or ""))
        p_ps1 = os.path.join(ctx.builddir, "launcher.ps1")
        with open(p_ps1, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(ps1)
        p_cmd = os.path.join(ctx.builddir, "Launcher %s.cmd" % ctx.name)
        with open(p_cmd, "w", encoding="utf-8", newline="") as f:
            f.write(CMD)
        if log:
            log("  launcher: %s" % os.path.basename(p_cmd))
        return p_cmd
    except OSError as e:
        if log:
            log("  launcher: nao consegui gravar (%s)" % e)
        return None
