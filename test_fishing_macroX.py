import json
import logging
import os
import tempfile
import unittest
import queue

import fishing_macroX as bot
from fishing_macroX import (
    WindowRect, WindowSnapshot, decidir_leitura_isca, decidir_ocr_fruta,
    janela_pronta, migrar_config_janela, mesma_janela, nome_bate_com_lista,
    normalizar_retangulo, validar_contexto_calibracao,
    point_from_pct, ponto_dentro_cliente, validar_ponto_pct,
    screen_point_from_pct,
)


class DecisaoOcrTest(unittest.TestCase):
    def test_ocr_vazio_pausa(self):
        resultado = decidir_ocr_fruta("", ["Mera"])
        self.assertEqual(resultado["acao"], "pausar")
        self.assertIn("vazio", resultado["motivo"])

    def test_ocr_incerto_com_lista_pausa(self):
        resultado = decidir_ocr_fruta("xyz", ["Mera"], limiar=0.9)
        self.assertEqual(resultado["acao"], "pausar")

    def test_nome_conhecido_descarta(self):
        resultado = decidir_ocr_fruta("Meraa", ["Mera"], limiar=0.6)
        self.assertEqual(resultado["acao"], "descartar")
        self.assertEqual(resultado["nome"], "Mera")

    def test_lista_vazia_permite_nome_confirmado(self):
        resultado = decidir_ocr_fruta("Mera", [])
        self.assertEqual(resultado["acao"], "guardar")
        self.assertEqual(resultado["nome"], "mera")

    def test_comparacao_pura_preserva_score(self):
        duplicata, nome, score = nome_bate_com_lista("Meraa", ["Mera"], 0.6)
        self.assertTrue(duplicata)
        self.assertEqual(nome, "Mera")
        self.assertGreaterEqual(score, 0.6)


class ConfiguracaoTest(unittest.TestCase):
    def test_defaults_e_regiao_invalida(self):
        cfg = bot.normalizar_config({
            "frutas_possuidas": "invalido",
            "regiao_slot_fruta": {"x1_pct": 0.8, "y1_pct": 0.1, "x2_pct": 0.2, "y2_pct": 0.4},
        })
        self.assertEqual(cfg["frutas_possuidas"], [])
        self.assertNotIn("regiao_slot_fruta", cfg)
        self.assertNotIn("tecla_guardar", cfg["fruta"])

    def test_salvamento_atômico_produz_json_completo(self):
        original = bot.CONFIG_PATH
        try:
            with tempfile.TemporaryDirectory() as pasta:
                bot.CONFIG_PATH = os.path.join(pasta, "config.json")
                cfg = bot.normalizar_config({"frutas_possuidas": ["Mera"]})
                fd, temporario = tempfile.mkstemp(dir=pasta)
                os.close(fd)
                os.unlink(temporario)
                app = bot.FishingBotApp.__new__(bot.FishingBotApp)
                app._salvar_config_completa(cfg)
                with open(bot.CONFIG_PATH, "r", encoding="utf-8") as arquivo:
                    self.assertEqual(json.load(arquivo)["frutas_possuidas"], ["Mera"])
        finally:
            bot.CONFIG_PATH = original

    def test_regiao_preserva_origem_do_monitor(self):
        regiao = bot.calcular_regiao_pixels(
            {"x1_pct": 0.1, "y1_pct": 0.2, "x2_pct": 0.4, "y2_pct": 0.5},
            1000, 800, origem_x=-1920, origem_y=40,
        )
        self.assertEqual(regiao["left"], -1820)
        self.assertEqual(regiao["top"], 200)

    def test_regiao_relativa_preserva_origem_negativa(self):
        cliente = WindowRect(-1920, 40, 1000, 800)
        regiao = point_from_pct(
            {"x1_pct": 0.1, "y1_pct": 0.2, "x2_pct": 0.4, "y2_pct": 0.5}, cliente
        )
        self.assertEqual(regiao.left, -1820)
        self.assertEqual(regiao.top, 200)
        self.assertTrue(ponto_dentro_cliente(type("P", (), {"x": -1800, "y": 250})(), cliente))

    def test_regiao_relativa_rejeita_percentuais_invalidos(self):
        with self.assertRaises(ValueError):
            point_from_pct({"x1_pct": 0.8, "y1_pct": 0, "x2_pct": 0.2, "y2_pct": 1}, WindowRect(0, 0, 100, 100))

    def test_ponto_store_fruit_preserva_origem_e_limites(self):
        cliente = WindowRect(-1200, 80, 1000, 800)
        ponto = {"x_pct": 0.25, "y_pct": 0.5}
        self.assertTrue(validar_ponto_pct(ponto))
        self.assertEqual(screen_point_from_pct(ponto, cliente), bot.ScreenPoint(-950, 480))
        self.assertFalse(validar_ponto_pct({"x_pct": 1.1, "y_pct": 0.5}))
        with self.assertRaises(ValueError):
            screen_point_from_pct({"x_pct": 1.1, "y_pct": 0.5}, cliente)

    def test_ponto_store_fruit_rejeita_contexto_dpi_incompativel(self):
        snapshot = WindowSnapshot(
            1, "Roblox", WindowRect(0, 0, 1000, 800),
            WindowRect(0, 0, 1920, 1080), 144, True, False,
        )
        self.assertFalse(validar_contexto_calibracao(
            {"janela_calibracao": {"width": 1000, "height": 800, "dpi": 96}},
            snapshot,
        ))

    def test_normalizacao_de_retangulo_limita_canvas_e_preserva_tamanho(self):
        self.assertEqual(
            normalizar_retangulo((120, -10, -20, 110), 100, 80),
            (0, 0, 100, 80),
        )
        self.assertEqual(normalizar_retangulo((50, 40, 50, 40), 100, 80), (50, 40, 51, 41))

    def test_migracao_de_monitor_para_cliente_com_origem_negativa(self):
        snapshot = WindowSnapshot(
            10, "Roblox", WindowRect(-1800, 100, 800, 600),
            WindowRect(-1920, 0, 1920, 1080), 120, True, False,
        )
        cfg, mudou = migrar_config_janela({
            "regiao_pesca": {"x1_pct": 0.05, "y1_pct": 0.1, "x2_pct": 0.15, "y2_pct": 0.2}
        }, snapshot)
        self.assertTrue(mudou)
        self.assertEqual(cfg["referencia_coordenadas"], "cliente")
        self.assertAlmostEqual(cfg["regiao_pesca"]["x1_pct"], 0.0)
        self.assertAlmostEqual(cfg["regiao_pesca"]["y1_pct"], 0.013333, places=4)
        self.assertEqual(cfg["janela_calibracao"]["dpi"], 120)

    def test_janela_pronta_e_comparacao_de_layout(self):
        snapshot = WindowSnapshot(1, "Roblox", WindowRect(0, 0, 100, 100),
                                  WindowRect(0, 0, 100, 100), 96, True, False)
        self.assertTrue(janela_pronta(snapshot))
        self.assertFalse(janela_pronta(WindowSnapshot(
            0, "fallback", snapshot.client, snapshot.monitor, 96, True, False)))
        self.assertTrue(mesma_janela(snapshot, snapshot))
        self.assertFalse(mesma_janela(snapshot, WindowSnapshot(
            1, "Roblox", WindowRect(0, 0, 101, 100),
            snapshot.monitor, 96, True, False)))

    def test_contexto_rejeita_dpi_incompativel(self):
        snapshot = WindowSnapshot(
            1, "Roblox", WindowRect(0, 0, 1000, 800),
            WindowRect(0, 0, 1920, 1080), 144, True, False,
        )
        self.assertFalse(validar_contexto_calibracao(
            {"janela_calibracao": {"width": 1000, "height": 800, "dpi": 96}},
            snapshot,
        ))


class MonitoramentoIscaTest(unittest.TestCase):
    def test_ocr_invalido_nao_conta_como_zero(self):
        resultado = decidir_leitura_isca(None, 2)
        self.assertEqual(resultado["acao"], "invalida")
        self.assertEqual(resultado["zeros_consecutivos"], 0)

    def test_zero_exige_tres_leituras_validas(self):
        self.assertEqual(decidir_leitura_isca(0, 0)["acao"], "aguardar")
        self.assertEqual(decidir_leitura_isca(0, 1)["acao"], "aguardar")
        self.assertEqual(decidir_leitura_isca(0, 2)["acao"], "acabou")

    def test_contador_positivo_reinicia_sequencia(self):
        resultado = decidir_leitura_isca(5, 2)
        self.assertEqual(resultado["acao"], "normal")
        self.assertEqual(resultado["zeros_consecutivos"], 0)


class MetricsTest(unittest.TestCase):
    def test_metricas_calculam_fps_e_eventos_por_hora(self):
        agora = [100.0]

        def clock():
            return agora[0]

        metricas = bot.BotMetrics(clock=clock)
        metricas.frame(120)
        metricas.fruta_detectada()
        agora[0] = 110.0
        resultado = metricas.snapshot()
        self.assertEqual(resultado["frames"], 120)
        self.assertEqual(resultado["eventos_slot"], 1)
        self.assertEqual(resultado["eventos_fruta"], 1)  # alias compatível
        self.assertEqual(resultado["fps"], 12.0)
        self.assertEqual(resultado["eventos_por_hora"], 360.0)

    def test_estados_sao_explicitamente_limitados(self):
        self.assertEqual(bot.BotState.PAUSADO_ISCA.value, "pausado - sem isca")
        self.assertNotIn("combate", [estado.value for estado in bot.BotState])


class FinalizacaoTest(unittest.TestCase):
    class Botao:
        def __init__(self):
            self.estados = []

        def config(self, **kwargs):
            self.estados.append(kwargs["state"])

    class Entrada:
        def __init__(self):
            self.mouse_up_chamadas = 0

        def mouse_up(self):
            self.mouse_up_chamadas += 1

    def test_excecao_de_worker_finaliza_e_reabilita_controles(self):
        app = bot.FishingBotApp.__new__(bot.FishingBotApp)
        app.macro_rodando = bot.threading.Event()
        app.pausar_para_fruta = bot.threading.Event()
        app.pausar_sem_isca = bot.threading.Event()
        app.macro_rodando.set()
        app._ui_queue = queue.Queue()
        app.input = self.Entrada()
        app.btn_iniciar = self.Botao()
        app.btn_areas = self.Botao()
        app.btn_cores = self.Botao()
        app.btn_baseline = self.Botao()
        app.btn_parar = self.Botao()
        app.btn_continuar_isca = self.Botao()
        estados = []
        app.set_status = lambda texto, cor: estados.append((texto, cor))
        app.log = lambda *args, **kwargs: None

        def worker(_cfg):
            raise RuntimeError("falha de teste")

        app._executar_worker(worker, {})
        self.assertFalse(app.macro_rodando.is_set())
        self.assertTrue(app.pausar_para_fruta.is_set())
        self.assertTrue(app.pausar_sem_isca.is_set())
        self.assertEqual(app.estado, bot.BotState.ERRO)
        self.assertEqual(app.input.mouse_up_chamadas, 1)
        while not app._ui_queue.empty():
            app._ui_queue.get_nowait()()
        self.assertEqual(app.btn_iniciar.estados, ["normal"])
        self.assertEqual(app.btn_parar.estados, ["disabled"])


class LoggingTest(unittest.TestCase):
    def test_fallback_de_logging_nao_lanca_quando_arquivo_e_inacessivel(self):
        original_path = bot.LOG_PATH
        original_configured = bot._LOGGING_CONFIGURED
        file_handlers = [handler for handler in bot.LOGGER.handlers
                         if isinstance(handler, logging.FileHandler)]
        try:
            bot.LOG_PATH = os.path.join(tempfile.gettempdir(), "diretorio-inexistente", "fishing.log")
            bot._LOGGING_CONFIGURED = False
            bot._ensure_logging_configured()
            self.assertTrue(bot._LOGGING_CONFIGURED)
            self.assertTrue(any(isinstance(handler, logging.StreamHandler)
                                for handler in bot.LOGGER.handlers))
        finally:
            for handler in list(bot.LOGGER.handlers):
                if isinstance(handler, logging.FileHandler) and handler not in file_handlers:
                    bot.LOGGER.removeHandler(handler)
                    handler.close()
            bot.LOG_PATH = original_path
            bot._LOGGING_CONFIGURED = original_configured

if __name__ == "__main__":
    unittest.main()
