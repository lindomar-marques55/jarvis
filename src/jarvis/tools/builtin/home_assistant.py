"""
Home Assistant Integration Tool for Jarvis.
Allows Jarvis to read sensor states, presence/people, and control smart home devices
(lights, switches, thermostats, scenes, scripts, etc.) via Home Assistant's REST API.
"""

import os
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional
import requests

from ..base import Tool, ToolContext
from ..types import ToolExecutionResult
from ...debug import debug_log

logger = logging.getLogger("jarvis.tools.home_assistant")

HA_CONFIG_PATH = Path.home() / ".config" / "jarvis" / "ha.json"


def get_ha_config() -> Dict[str, str]:
    """Retrieve Home Assistant URL and Bearer Token from file or environment."""
    config = {
        "url": os.environ.get("HA_URL", "").rstrip("/"),
        "token": os.environ.get("HA_TOKEN", ""),
        "alarm_code": os.environ.get("HA_ALARM_CODE", "")
    }

    if HA_CONFIG_PATH.exists():
        try:
            with open(HA_CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if saved.get("url"):
                    config["url"] = saved["url"].rstrip("/")
                if saved.get("token"):
                    config["token"] = saved["token"]
                if saved.get("alarm_code"):
                    config["alarm_code"] = str(saved["alarm_code"])
        except Exception as e:
            debug_log(f"Error reading HA config: {e}", "home_assistant")

    return config


class HomeAssistantTool(Tool):
    """Tool for controlling and querying Home Assistant."""

    @property
    def name(self) -> str:
        return "homeAssistant"

    @property
    def description(self) -> str:
        return (
            "Control and inspect smart home devices, presence, alarm, and sensors in Home Assistant. "
            "Use this tool whenever the user asks: "
            "1. Arm alarm ('ativar alarme de ausente', 'armar ausente', 'armar em casa', 'ligar alarme') -> action: 'arm_alarm', mode: 'away' (ausente) or 'home' (em casa); "
            "2. Disarm alarm ('desativar alarme', 'desarmar o alarme', 'desligar alarme', 'desative') -> action: 'disarm_alarm'; "
            "3. Alarm status ('o alarme está acionado?', 'o alarme está armado?', 'como está o alarme?') -> action: 'get_alarm_state'; "
            "4. Who is at home or how many people are in the house ('quem está em casa?', 'quantas pessoas em casa?') -> action: 'get_people_home'; "
            "5. Turn on/off lights, switches, scenes, relays ('apague a luz', 'ligue o interruptor') -> action: 'call_service'; "
            "6. Check temperature, humidity, energy, sensor readings, device states, gate status, lock status "
            "('qual a temperatura?', 'o portão está aberto?', 'a tranca está fechada?') -> action: 'get_state' with entity_id. "
            "If the exact entity_id is not known, use action: 'find_entity' with a keyword to search. "
            "7. List available devices in the house -> action: 'list_entities'. "
            "IMPORTANT: NEVER guess or invent a state. Always call this tool to get real-time data from Home Assistant."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["arm_alarm", "disarm_alarm", "get_alarm_state", "get_people_home", "call_service", "get_state", "find_entity", "list_entities"],
                    "description": (
                        "Action to perform: "
                        "'arm_alarm' to arm the house alarm in away (ausente) or home (em casa) mode, "
                        "'disarm_alarm' to disarm/deactivate the house alarm, "
                        "'get_alarm_state' to check if the house alarm is armed, disarmed, or triggered, "
                        "'get_people_home' to check who is currently at home or how many people are home, "
                        "'find_entity' to search for an entity by keyword when the exact entity_id is unknown (e.g. keyword='temperatura sala'), "
                        "'get_state' to check a specific entity's current state (requires entity_id), "
                        "'call_service' to turn devices on/off or toggle them, "
                        "'list_entities' to discover available devices."
                    )
                },
                "mode": {
                    "type": "string",
                    "enum": ["away", "home", "night"],
                    "description": "Alarm mode for arm_alarm action: 'away' (armar ausente) or 'home' (armar em casa)."
                },
                "code": {
                    "type": "string",
                    "description": "Optional PIN code for disarming the alarm; if omitted, Jarvis uses the configured PIN when one exists."
                },
                "keyword": {
                    "type": "string",
                    "description": "Keyword to search for entities by name (used with find_entity action). Example: 'alarme', 'temperatura', 'portao', 'luz sala'."
                },
                "domain": {
                    "type": "string",
                    "description": "Entity domain (e.g., 'light', 'switch', 'climate', 'sensor', 'person', 'zone', 'alarm_control_panel')."
                },
                "service": {
                    "type": "string",
                    "description": "Service name to call (e.g., 'turn_on', 'turn_off', 'toggle', 'set_temperature'). Required for call_service."
                },
                "entity_id": {
                    "type": "string",
                    "description": "Entity identifier (e.g., 'light.living_room', 'alarm_control_panel.alarme_residencial'). Required for get_state and call_service."
                },
                "data": {
                    "type": "object",
                    "description": "Optional parameters to pass to the service call (e.g. {'brightness': 200}, {'temperature': 24})."
                }
            },
            "required": ["action"]
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        if not args:
            return ToolExecutionResult(success=False, reply_text="Nenhum argumento fornecido para o Home Assistant.")

        ha_cfg = get_ha_config()
        url = ha_cfg["url"]
        token = ha_cfg["token"]

        if not token:
            context.user_print("⚠️ Home Assistant não configurado com token de acesso.")
            return ToolExecutionResult(
                success=False,
                reply_text=(
                    "O Home Assistant ainda não foi autenticado. Por favor, adicione seu Token de Longa Duração "
                    "(Long-Lived Access Token) nas configurações da interface web do Jarvis."
                )
            )

        action = args.get("action", "")
        # Handle aliases
        if action in ("who_is_home", "check_presence", "people"):
            action = "get_people_home"
        elif action in ("check_alarm", "alarm_status", "alarm_state", "is_alarm_on", "get_alarm", "alarm"):
            action = "get_alarm_state"
        elif action in ("arm", "arm_away", "arm_home", "activate_alarm", "ligar_alarme"):
            action = "arm_alarm"
        elif action in ("disarm", "deactivate_alarm", "desligar_alarme", "desarmar_alarme"):
            action = "disarm_alarm"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            # Action: Arm house alarm (away or home)
            if action == "arm_alarm":
                mode = str(args.get("mode", "away")).lower()
                entity_id = args.get("entity_id", "alarm_control_panel.alarme_residencial")

                if "home" in mode or "casa" in mode:
                    service = "alarm_arm_home"
                    mode_desc = "modo casa"
                    extra = " (sensores perimetrais ativos)"
                elif "night" in mode or "noite" in mode:
                    service = "alarm_arm_night"
                    mode_desc = "modo noite"
                    extra = ""
                else:
                    service = "alarm_arm_away"
                    mode_desc = "modo ausente"
                    extra = " (você tem 30 segundos para sair)"

                context.user_print(f"🛡️ Home Assistant: Armando alarme em {mode_desc}...")
                req_url = f"{url}/api/services/alarm_control_panel/{service}"
                payload = {"entity_id": entity_id}
                res = requests.post(req_url, headers=headers, json=payload, timeout=8.0)
                if res.status_code in (200, 201):
                    return ToolExecutionResult(
                        success=True,
                        reply_text=f"O alarme residencial foi armado com sucesso em {mode_desc}{extra}."
                    )
                else:
                    return ToolExecutionResult(
                        success=False,
                        reply_text=f"Erro ao armar alarme no Home Assistant: HTTP {res.status_code} - {res.text}"
                    )

            # Action: Disarm house alarm
            elif action == "disarm_alarm":
                entity_id = args.get("entity_id", "alarm_control_panel.alarme_residencial")
                code = args.get("code") or ha_cfg.get("alarm_code", "")

                context.user_print("🛡️ Home Assistant: Desarmando alarme residencial...")
                req_url = f"{url}/api/services/alarm_control_panel/alarm_disarm"
                payload = {"entity_id": entity_id}
                if code:
                    payload["code"] = str(code)

                res = requests.post(req_url, headers=headers, json=payload, timeout=8.0)
                if res.status_code in (200, 201):
                    return ToolExecutionResult(
                        success=True,
                        reply_text="O alarme residencial foi desarmado com sucesso."
                    )
                else:
                    return ToolExecutionResult(
                        success=False,
                        reply_text=f"Erro ao desarmar alarme no Home Assistant: HTTP {res.status_code} - {res.text}"
                    )

            # Action: Check house alarm state
            elif action == "get_alarm_state" or (action == "get_state" and args.get("entity_id", "").lower() in ("alarm", "alarme", "alarm_control_panel")):
                context.user_print("🛡️ Verificando estado do alarme no Home Assistant...")
                req_url = f"{url}/api/states"
                res = requests.get(req_url, headers=headers, timeout=8.0)
                if res.status_code == 200:
                    all_states = res.json()
                    alarm_entities = [
                        s for s in all_states
                        if s.get("entity_id", "").startswith("alarm_control_panel.")
                        and s.get("state") not in ("unavailable", "unknown")
                    ]
                    if not alarm_entities:
                        alarm_entities = [s for s in all_states if s.get("entity_id", "").startswith("alarm_control_panel.")]

                    if not alarm_entities:
                        return ToolExecutionResult(
                            success=True,
                            reply_text="Nenhum painel de alarme configurado foi encontrado no Home Assistant."
                        )

                    state_translations = {
                        "disarmed": "desarmado (desativado)",
                        "armed_home": "armado em modo casa (ativo)",
                        "armed_away": "armado em modo ausente (ativo)",
                        "armed_night": "armado em modo noite (ativo)",
                        "armed_vacation": "armado em modo férias (ativo)",
                        "armed_custom_bypass": "armado com bypass (ativo)",
                        "arming": "em processo de armar",
                        "disarming": "em processo de desarmar",
                        "triggered": "DISPARADO!",
                        "pending": "pendente",
                        "unavailable": "indisponível",
                    }

                    reports = []
                    for a in alarm_entities:
                        st = a.get("state", "").lower()
                        st_pt = state_translations.get(st, st)
                        fname = a.get("attributes", {}).get("friendly_name", a["entity_id"])
                        reports.append(f"O alarme residencial '{fname}' ({a['entity_id']}) está atualmente {st_pt}.")

                    return ToolExecutionResult(success=True, reply_text=" ".join(reports))
                else:
                    return ToolExecutionResult(success=False, reply_text=f"Erro ao consultar alarme: HTTP {res.status_code}")
            # 0. Action: Find entity by keyword (search)
            if action == "find_entity":
                keyword = args.get("keyword", "").strip().lower()
                if not keyword:
                    return ToolExecutionResult(success=False, reply_text="Forneça um 'keyword' para buscar a entidade.")

                context.user_print(f"🔎 Buscando entidade '{keyword}' no Home Assistant...")
                req_url = f"{url}/api/states"
                res = requests.get(req_url, headers=headers, timeout=8.0)
                if res.status_code == 200:
                    all_states = res.json()
                    matches = []
                    for item in all_states:
                        eid = item.get("entity_id", "")
                        fname = item.get("attributes", {}).get("friendly_name", "").lower()
                        if keyword in eid.lower() or keyword in fname:
                            state = item.get("state", "?")
                            attrs = item.get("attributes", {})
                            unit = attrs.get("unit_of_measurement", "")
                            display = attrs.get("friendly_name", eid)
                            matches.append(f"{display} (`{eid}`): {state} {unit}".strip())

                    if not matches:
                        return ToolExecutionResult(
                            success=True,
                            reply_text=f"Nenhuma entidade encontrada com o termo '{keyword}' no Home Assistant."
                        )
                    summary = "\n".join(matches[:10])
                    if len(matches) > 10:
                        summary += f"\n... e mais {len(matches) - 10} entidades."
                    return ToolExecutionResult(
                        success=True,
                        reply_text=f"Entidades encontradas para '{keyword}':\n{summary}"
                    )
                else:
                    return ToolExecutionResult(success=False, reply_text=f"Erro ao buscar entidades: HTTP {res.status_code}")

            # 1. Action: Check who is at home
            if action == "get_people_home" or (action == "list_entities" and args.get("domain", "").lower() in ("person", "people", "zone")):
                context.user_print("👥 Verificando presença e pessoas em casa no Home Assistant...")
                req_url = f"{url}/api/states"
                res = requests.get(req_url, headers=headers, timeout=8.0)
                if res.status_code == 200:
                    all_states = res.json()
                    home_people = []
                    away_people = []
                    for item in all_states:
                        eid = item.get("entity_id", "")
                        if eid.startswith("person."):
                            name = item.get("attributes", {}).get("friendly_name", eid.replace("person.", ""))
                            state = item.get("state", "").lower()
                            if state == "home":
                                home_people.append(name)
                            else:
                                away_people.append(f"{name} ({state})")

                    count = len(home_people)
                    if count == 0:
                        reply = "No Home Assistant, consta que não há ninguém em casa no momento."
                    else:
                        reply = f"No Home Assistant constam {count} pessoa(s) em casa no momento: {', '.join(home_people)}."

                    if away_people:
                        reply += f" Fora de casa: {', '.join(away_people)}."

                    return ToolExecutionResult(success=True, reply_text=reply)
                else:
                    return ToolExecutionResult(
                        success=False,
                        reply_text=f"Erro ao consultar pessoas no Home Assistant: HTTP {res.status_code}"
                    )

            # 2. Action: Call Service (turn on/off, toggle)
            elif action == "call_service":
                domain = args.get("domain", "")
                service = args.get("service", "")
                entity_id = args.get("entity_id", "")
                payload = args.get("data") or {}

                if entity_id:
                    payload["entity_id"] = entity_id
                    if not domain and "." in entity_id:
                        domain = entity_id.split(".")[0]

                if not domain or not service:
                    return ToolExecutionResult(
                        success=False,
                        reply_text="Para acionar um serviço, forneça 'domain' e 'service' (ou 'entity_id' completo)."
                    )

                context.user_print(f"🏠 Home Assistant: Acionando {domain}.{service} em {entity_id or 'todos'}...")
                req_url = f"{url}/api/services/{domain}/{service}"
                res = requests.post(req_url, headers=headers, json=payload, timeout=8.0)
                if res.status_code in (200, 201):
                    return ToolExecutionResult(
                        success=True,
                        reply_text=f"Serviço '{domain}.{service}' executado com sucesso no Home Assistant."
                    )
                else:
                    return ToolExecutionResult(
                        success=False,
                        reply_text=f"Erro ao chamar Home Assistant: HTTP {res.status_code} - {res.text}"
                    )

            # 3. Action: Get State of an entity
            elif action == "get_state":
                entity_id = args.get("entity_id", "")
                if not entity_id:
                    return ToolExecutionResult(success=False, reply_text="Informe o 'entity_id' para consultar o estado.")

                context.user_print(f"🔍 Consultando estado de {entity_id} no Home Assistant...")
                req_url = f"{url}/api/states/{entity_id}"
                res = requests.get(req_url, headers=headers, timeout=8.0)
                if res.status_code == 200:
                    data = res.json()
                    state = data.get("state", "desconhecido")
                    attrs = data.get("attributes", {})
                    friendly_name = attrs.get("friendly_name", entity_id)
                    unit = attrs.get("unit_of_measurement", "")

                    if entity_id == "zone.home":
                        persons = attrs.get("persons", [])
                        return ToolExecutionResult(
                            success=True,
                            reply_text=f"A zona da Casa (zone.home) indica {state} pessoa(s) em casa no momento. Pessoas: {', '.join(persons)}."
                        )
                    elif entity_id.startswith("person."):
                        status_str = "em casa (home)" if state.lower() == "home" else f"fora de casa ({state})"
                        return ToolExecutionResult(
                            success=True,
                            reply_text=f"{friendly_name} está {status_str}."
                        )

                    return ToolExecutionResult(
                        success=True,
                        reply_text=f"O estado de {friendly_name} ({entity_id}) é: {state} {unit}".strip()
                    )
                else:
                    return ToolExecutionResult(
                        success=False,
                        reply_text=f"Entidade '{entity_id}' não encontrada no Home Assistant (HTTP {res.status_code})."
                    )

            # 4. Action: List Entities
            elif action == "list_entities":
                domain_filter = args.get("domain", "").lower()
                context.user_print("📋 Listando entidades do Home Assistant...")
                req_url = f"{url}/api/states"
                res = requests.get(req_url, headers=headers, timeout=8.0)
                if res.status_code == 200:
                    all_states = res.json()
                    filtered = []
                    for item in all_states:
                        eid = item.get("entity_id", "")
                        if not domain_filter or eid.startswith(f"{domain_filter}."):
                            name = item.get("attributes", {}).get("friendly_name", eid)
                            state = item.get("state", "")
                            filtered.append(f"{name} (`{eid}`): {state}")

                    summary = "\n".join(filtered[:30])
                    if len(filtered) > 30:
                        summary += f"\n... e mais {len(filtered) - 30} entidades."

                    return ToolExecutionResult(
                        success=True,
                        reply_text=f"Entidades encontradas no Home Assistant:\n{summary}"
                    )
                else:
                    return ToolExecutionResult(
                        success=False,
                        reply_text=f"Erro ao listar entidades do Home Assistant: HTTP {res.status_code}"
                    )

            else:
                return ToolExecutionResult(success=False, reply_text=f"Ação desconhecida: {action}")

        except Exception as e:
            debug_log(f"Home Assistant exception: {e}", "home_assistant")
            return ToolExecutionResult(success=False, reply_text=f"Falha na comunicação com o Home Assistant: {str(e)}")
