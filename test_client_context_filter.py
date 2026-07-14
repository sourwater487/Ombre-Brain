from datetime import datetime, timedelta, timezone

from gateway import GatewayService
from raw_events import RawEventStore, strip_raw_client_context


WEB_REQUEST_TEXT = """<dynamic_context>
<memo_context>Che memo that is not Lin's utterance</memo_context>
<current_time>2026-07-14T21:05:00+08:00</current_time>
</dynamic_context>

<lin_message>
[2026-07-14 21:05 | 小雨 28℃ | 上海市徐汇区]
我真正说的话
</lin_message>"""


def test_web_request_keeps_only_the_lin_utterance():
    assert strip_raw_client_context(WEB_REQUEST_TEXT) == "我真正说的话"


def test_standalone_client_metadata_xml_is_removed():
    text = """<timestamp>2026-07-14T21:05:00+08:00</timestamp>
<weather>小雨 28℃</weather>
<location>上海市徐汇区</location>
<dynamic>injected activity</dynamic>
用户正文"""

    assert strip_raw_client_context(text) == "用户正文"


def test_legacy_message_time_suffix_is_removed():
    text = "用户正文\n\n<message_time timezone=\"Asia/Shanghai\">2026/07/14 21:05</message_time>"

    assert strip_raw_client_context(text) == "用户正文"


def test_context_only_input_does_not_turn_into_a_recall_query():
    text = "<dynamic_context><memo_context>memo</memo_context></dynamic_context>"

    assert strip_raw_client_context(text) == ""


def test_reserved_context_xml_inside_lin_message_is_preserved_as_user_text():
    text = "<lin_message>请解释 <dynamic_context>foo</dynamic_context> 这个 XML</lin_message>"

    assert strip_raw_client_context(text) == "请解释 <dynamic_context>foo</dynamic_context> 这个 XML"


def test_unrelated_xml_is_not_removed():
    text = "请分析 <example kind=\"demo\">正文</example>"

    assert strip_raw_client_context(text) == text


def test_lin_message_mentioned_inside_ordinary_prose_is_not_an_envelope():
    text = "请解释 <lin_message>foo</lin_message> 这个 XML"

    assert strip_raw_client_context(text) == text


def test_gateway_current_turn_query_uses_the_same_filter():
    service = object.__new__(GatewayService)

    assert service._extract_current_turn_user_query(
        [{"role": "user", "content": WEB_REQUEST_TEXT}]
    ) == "我真正说的话"


def test_gateway_filters_split_text_blocks_around_an_image():
    service = object.__new__(GatewayService)
    content = [
        {"type": "text", "text": "<dynamic_context>memo</dynamic_context>\n<lin_message>\n"},
        {"type": "text", "text": "[2026-07-14 21:05 | 小雨 28℃ | 上海]\n带图的一句话"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        {"type": "text", "text": "\n</lin_message>"},
    ]

    assert service._extract_current_turn_user_query(
        [{"role": "user", "content": content}]
    ) == "带图的一句话"


def test_gateway_tool_continuation_is_not_treated_as_a_new_user_turn():
    service = object.__new__(GatewayService)
    messages = [
        {"role": "user", "content": WEB_REQUEST_TEXT},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "tool result"},
    ]

    assert service._extract_current_turn_user_query(messages) == ""


def test_gateway_stored_turn_cleanup_is_role_aware():
    service = object.__new__(GatewayService)
    cleaned_by_persona = []

    class PersonaCleaner:
        @staticmethod
        def _clean_client_status_lines(text):
            cleaned_by_persona.append(text)
            return text

    service.persona_engine = PersonaCleaner()
    assistant_text = "示例：<lin_message>foo</lin_message>"

    assert service._clean_conversation_turn_text(
        WEB_REQUEST_TEXT,
        role="user",
    ) == "我真正说的话"
    assert service._clean_conversation_turn_text(
        assistant_text,
        role="assistant",
    ) == assistant_text
    assert cleaned_by_persona == ["我真正说的话"]


def test_raw_event_ingest_stores_only_the_user_utterance(tmp_path):
    store = RawEventStore({"state_dir": str(tmp_path)})
    result = store.ingest(
        [
            {
                "role": "user",
                "text": WEB_REQUEST_TEXT,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
        source="test",
    )

    assert result["inserted"] == 1
    events = store.list_events_between(
        start_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        end_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    assert [event["text"] for event in events] == ["我真正说的话"]


def test_raw_event_ingest_preserves_assistant_xml_examples(tmp_path):
    store = RawEventStore({"state_dir": str(tmp_path)})
    assistant_text = "示例：<lin_message>foo</lin_message>"
    result = store.ingest(
        [{"role": "assistant", "text": assistant_text}],
        source="test",
    )

    assert result["inserted"] == 1
    events = store.list_events_between(
        start_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        end_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    assert [event["text"] for event in events] == [assistant_text]
