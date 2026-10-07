# Сохранность, обновление и восстановление

Команды выполняет Codex из активного app, указанного в `~/Library/Application Support/AndroidPublisher/runtime/active.json`. Данные и installation.json находятся отдельно, в data из active.json; Keychain service сохраняется. Не удаляй их ради переустановки и не меняй физический serial/UC ID.

Перед изменением: status → backup в новый приватный каталог → pause → stop → сверка неизвестных операций. `backup /absolute/new-directory` сохраняет согласованные SQLite/settings/identity с хешами, без экспорта Keychain. `restore --snapshot /path` сперва сохраняет текущее состояние; восстановленные внешние эффекты требуют readback, очередь остаётся на паузе. Секреты не переносить через Git.

Обновление только по поручению: получи отдельный чистый checkout публичного источника, проверь PACKAGE.sha256.json, затем `update --package /absolute/verified-checkout`. Оно сохраняет snapshot и previous runtime, проверяет новую службу под launchd и doctor. При отказе возвращает старый runtime с сохранённой очередью на паузе. Эта команда устанавливает/запускает LaunchAgents, поэтому **не применяй её к намеренно остановленной службе без разрешения возобновить процесс**. Доставку исходника можно проверить, оставив старый runtime остановленным.

`rollback` сохраняет нынешнюю очередь, проверяет совместимость предыдущей версии, переключает runtime и тоже запускает LaunchAgents; очередь остаётся на паузе до doctor/readback. Не откатывай код к несовместимой схеме. Ни update, ни rollback не требуют плагина, каталога или изменения config/cache Codex. Не вызывай prepare-catalog.

Потеря телефона: status/doctor → причина LAN/ADB/Internet → проверка того же serial → verify-phone/probe-phone. После reboot могут потребоваться ручное разблокирование и USB Allow/tcpip. Не повторяй upload при неизвестном результате. Для истёкшего входа — telegram-login; для отказа Keychain — keychain-authorize в foreground и background-preflight. Старую службу параллельно не запускать.

Для прежнего контура используйте import-legacy только после исследования/снимка и остановки старого runner; не объединяйте две живые очереди. `stop` останавливает службу; `remove-service` убирает LaunchAgents, сохраняя данные. Возобновление — только после согласованных проверок, `install-service` и doctor, затем resume. Новые чаты читают AGENTS.md этого проекта и фактические status/журналы; память старого чата не является состоянием службы.
