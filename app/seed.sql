INSERT INTO knowledge_versions (version_number, is_active)
VALUES (1, TRUE) ON CONFLICT (version_number) DO NOTHING;

INSERT INTO questions (code, prompt, sort_order) VALUES
 ('outage', 'Есть ли информация о массовой аварии в вашем районе?', 1),
 ('power', 'Маршрутизатор включён?', 2),
 ('wan', 'Горит ли индикатор WAN или Internet на маршрутизаторе?', 3),
 ('wifi', 'Проблема возникает только при подключении по Wi-Fi?', 4),
 ('devices', 'Проблема затрагивает несколько устройств?', 5)
ON CONFLICT (code) DO NOTHING;

INSERT INTO answer_options (question_id, code, label, sort_order)
SELECT q.id, o.code, o.label, o.sort_order
FROM questions q
CROSS JOIN (VALUES ('yes','Да',1),('no','Нет',2),('unknown','Не знаю',3)) o(code,label,sort_order)
WHERE q.code IN ('outage','power','wan','wifi','devices')
ON CONFLICT (question_id, code) DO NOTHING;

INSERT INTO fault_types (code, title) VALUES
 ('provider_outage','Авария на стороне провайдера'),
 ('router_power','Маршрутизатор не получает питание'),
 ('wan_connection','Проблема с WAN-подключением'),
 ('wifi_issue','Проблема с Wi-Fi'),
 ('one_device','Проблема отдельного устройства')
ON CONFLICT (code) DO NOTHING;

INSERT INTO production_rules
 (code, knowledge_version_id, fault_type_id, priority, recommendation, explanation, escalation_required)
SELECT r.code, kv.id, f.id, r.priority, r.recommendation, r.explanation, r.escalation_required
FROM (VALUES
 ('outage_rule','provider_outage',10,'Сообщите абоненту об аварии и передайте обращение второй линии.','Зафиксирована массовая авария в районе абонента.',TRUE),
 ('power_rule','router_power',20,'Проверьте адаптер питания и розетку, затем включите маршрутизатор.','Маршрутизатор не включён.',FALSE),
 ('wan_rule','wan_connection',30,'Проверьте WAN-кабель и перезапустите маршрутизатор. Если связь не появится, передайте заявку второй линии.','Индикатор WAN не горит.',FALSE),
 ('wifi_rule','wifi_issue',40,'Проверьте подключение по Wi-Fi и перезапустите маршрутизатор.','Неисправность проявляется только по Wi-Fi.',FALSE),
 ('device_rule','one_device',50,'Проверьте сетевые настройки и перезапустите проблемное устройство.','Не работает только одно устройство.',FALSE)
) r(code,fault_code,priority,recommendation,explanation,escalation_required)
JOIN knowledge_versions kv ON kv.version_number=1
JOIN fault_types f ON f.code=r.fault_code
ON CONFLICT (code) DO NOTHING;

INSERT INTO rule_conditions (rule_id, question_id, answer_code)
SELECT r.id, q.id, c.answer_code
FROM (VALUES
 ('outage_rule','outage','yes'),
 ('power_rule','power','no'),
 ('wan_rule','wan','no'),
 ('wifi_rule','wifi','yes'),
 ('device_rule','devices','no')
) c(rule_code,question_code,answer_code)
JOIN production_rules r ON r.code=c.rule_code
JOIN questions q ON q.code=c.question_code
ON CONFLICT (rule_id, question_id) DO NOTHING;
