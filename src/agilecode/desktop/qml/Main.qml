import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import AgileCode.Desktop 1.0

ApplicationWindow {
    id: root
    width: 1450; height: 940
    minimumWidth: 1050; minimumHeight: 740
    visible: true
    title: "AgileCode — Ticket workbench"
    font.family: "Sans Serif"
    property bool dark: true
    property color bg: dark ? "#101923" : "#f3f6f9"
    property color panel: dark ? "#16232f" : "#ffffff"
    property color borderColor: dark ? "#2c4152" : "#d9e2ea"
    property color ink: dark ? "#e3ebf3" : "#223647"
    property color muted: dark ? "#94aabe" : "#607789"
    property var tickets: []
    property var teams: []
    property var skills: []
    property var settings: ({})
    property var detail: ({})
    property string projectPath: "Opening project…"
    property string selectedTicket: ""
    property bool ticketDescriptionExpanded: false
    property string selectedSession: ""
    property var transcript: []
    property string conversationRequestId: ""
    property string conversationOlderRequestId: ""
    property bool conversationHasOlder: false
    property var fileList: []
    property var models: []
    property var recovery: ({tools: [], jobs: [], operations: []})
    property string statusMessage: "Starting headless engine…"
    property int currentPage: 0
    property bool engineReady: false
    property string pendingQuestion: ""
    property var pendingRestore: ({})
    property var pendingMerge: ({})
    property var pendingReconcile: ({})
    property string selectedJob: ""
    property var discussions: []
    property var selectedDiscussion: ({})
    property var discussionTranscript: []
    property string discussionHistoryRequestId: ""
    property bool discussionBusy: false
    property string generatingDiscussionId: ""
    property var generatingRuns: ({})
    property string pendingSourceDiscussion: ""
    property string pendingSourceDiscussionTitle: ""
    property string pendingDraftTitle: ""
    property string pendingDraftDescription: ""
    property bool ticketDraftBusy: false
    property string ticketDraftRequestId: ""
    property bool closingConfirmed: false
    property var commandRulesDraft: []
    property var states: ["Backlog", "Ready", "In Progress", "Done"]
    color: bg
    palette.window: panel
    palette.base: dark ? "#101c27" : "#ffffff"
    palette.text: ink
    palette.windowText: ink
    palette.button: dark ? "#243744" : "#e7eef4"
    palette.buttonText: ink
    palette.highlight: "#406959"
    palette.highlightedText: "#ffffff"

    MarkdownPresentation { id: markdownPresentation }

    function request(action, data) { return backend.dispatch(action, JSON.stringify(data || {})); }
    function validTeams() { return teams.filter(function(item) { return item.team !== undefined; }); }
    function setRunGenerating(runId, active) {
        var updated = ({});
        Object.keys(generatingRuns).forEach(function(id) { updated[id] = generatingRuns[id]; });
        if (active) updated[runId] = true;
        else delete updated[runId];
        generatingRuns = updated;
    }
    function refreshDiscussions() {
        if (engineReady) request("discussion_list", {archived: showArchivedDiscussions.checked});
    }
    function selectDiscussion(discussion) {
        selectedDiscussion = discussion || ({});
        discussionTranscript = [];
        discussionHistoryRequestId = "";
        discussionView.resetLive();
        discussionView.sessionId = discussion && discussion.id ? discussion.session_id : "";
        if (discussion && discussion.id)
            discussionHistoryRequestId = request("discussion_history", {discussion: discussion.id});
    }
    function prepareNewTicket(sourceDiscussion, sourceTitle, draftTitle, draftDescription) {
        pendingSourceDiscussion = sourceDiscussion || "";
        pendingSourceDiscussionTitle = sourceTitle || "";
        pendingDraftTitle = draftTitle || "";
        pendingDraftDescription = markdownPresentation.plainText(draftDescription || "");
        newTicketDialog.open();
    }
    function filtered(state) {
        var term = searchField.text.toLowerCase();
        return tickets.filter(function(ticket) { return ticket.state === state && (ticket.title.toLowerCase().indexOf(term) >= 0 || ticket.key.toLowerCase().indexOf(term) >= 0); });
    }
    function openTicket(identity) { if (selectedTicket !== identity) { detail = ({}); ticketDescriptionExpanded = false; selectedSession = ""; transcript = []; conversationRequestId = ""; conversationOlderRequestId = ""; conversationHasOlder = false; fileList = []; selectedJob = ""; } selectedTicket = identity; request("detail", {ticket: identity}); inspector.open(); }
    function refresh() { if (engineReady) { request("board", {archived: showArchived.checked}); if (selectedTicket) request("detail", {ticket: selectedTicket}); } }
    function selectSession(identity) {
        transcript = [];
        selectedSession = identity;
        conversationRequestId = "";
        conversationOlderRequestId = "";
        conversationHasOlder = false;
        conversationView.resetLive();
        loadConversation();
        inspectTabs.currentIndex = 2;
    }
    function mergeConversationRows(existing, incoming) {
        var byId = ({});
        existing.forEach(function(row) { byId[row.id || String(row.seq)] = row; });
        incoming.forEach(function(row) { byId[row.id || String(row.seq)] = row; });
        return Object.keys(byId).map(function(key) { return byId[key]; }).sort(function(a, b) { return a.seq - b.seq; });
    }
    function loadConversation() {
        if (!selectedSession) return;
        conversationRequestId = request("conversation", {session: selectedSession, before: null});
    }
    function loadOlderConversation() {
        if (!selectedSession || transcript.length === 0 || !conversationHasOlder || conversationOlderRequestId.length > 0) return;
        conversationOlderRequestId = request("conversation", {session: selectedSession, before: transcript[0].seq});
    }
    function updateCommandRule(index, field, value) {
        var rules = JSON.parse(JSON.stringify(commandRulesDraft));
        rules[index][field] = value;
        commandRulesDraft = rules;
    }
    function addCommandRule() {
        var rules = JSON.parse(JSON.stringify(commandRulesDraft));
        rules.push({pattern: "", action: "ask"});
        commandRulesDraft = rules;
    }
    function removeCommandRule(index) {
        var rules = JSON.parse(JSON.stringify(commandRulesDraft));
        rules.splice(index, 1);
        commandRulesDraft = rules;
    }
    function saveBasicSettings() {
        var updated = JSON.parse(JSON.stringify(settings));
        var profile = updated.profiles[updated.active_profile];
        profile.base_url = endpointField.text.trim(); profile.model = modelField.text.trim() || null;
        profile.api_key_env = keyField.text.trim() || null;
        profile.timeout_seconds = modelTimeoutField.text.trim().length > 0 ? Number(modelTimeoutField.text) : null;
        updated.shell_command_timeout_seconds = shellTimeoutField.text.trim().length > 0 ? Number(shellTimeoutField.text) : null;
        updated.max_corrections = Number(correctionLimitField.text);
        updated.autonomy = autonomySwitch.checked ? "full" : "restricted";
        updated.merge_on_completion = mergeSwitch.checked; updated.theme = lightSwitch.checked ? "light" : "dark";
        updated.open_reasoning_by_default = reasoningOpenSwitch.checked;
        updated.command_rules = JSON.parse(JSON.stringify(commandRulesDraft)).filter(function(rule) { return rule.pattern.trim().length > 0; });
        updated.verification_commands = verifyField.text.split("\n").filter(function(t) { return t.trim().length > 0; });
        var available = validTeams();
        if (defaultTeam.currentIndex >= 0 && defaultTeam.currentIndex < available.length) {
            updated.default_team = available[defaultTeam.currentIndex].ref;
            updated.default_process = available[defaultTeam.currentIndex].team.processes[defaultProcess.currentIndex].id;
        }
        request("settings", {settings: updated, scope: settingsScope.currentIndex === 0 ? "project" : "global"});
    }
    function loadSettings() {
        if (!settings.profiles) return;
        var profile = settings.profiles[settings.active_profile];
        endpointField.text = profile.base_url; modelField.text = profile.model || ""; keyField.text = profile.api_key_env || "";
        modelTimeoutField.text = profile.timeout_seconds === null || profile.timeout_seconds === undefined ? "" : String(profile.timeout_seconds);
        shellTimeoutField.text = settings.shell_command_timeout_seconds === null || settings.shell_command_timeout_seconds === undefined ? "" : String(settings.shell_command_timeout_seconds);
        correctionLimitField.text = String(settings.max_corrections);
        autonomySwitch.checked = settings.autonomy === "full"; mergeSwitch.checked = settings.merge_on_completion;
        lightSwitch.checked = settings.theme === "light"; reasoningOpenSwitch.checked = !!settings.open_reasoning_by_default; verifyField.text = settings.verification_commands.join("\n");
        commandRulesDraft = JSON.parse(JSON.stringify(settings.command_rules || []));
        defaultTeam.currentIndex = Math.max(0, validTeams().findIndex(function(t) { return t.ref === settings.default_team; }));
        Qt.callLater(function() { var t = root.validTeams()[defaultTeam.currentIndex]; if (t) defaultProcess.currentIndex = Math.max(0, t.team.processes.findIndex(function(p) { return p.id === root.settings.default_process; })); });
    }
    onClosing: function(close) { if (!closingConfirmed) { close.accepted = false; quitDialog.open(); } }
    Timer { id: conversationRefresh; interval: 80; onTriggered: root.loadConversation() }
    Timer { id: refreshTimer; interval: 300; onTriggered: root.refresh() }
    Timer { interval: 3000; running: root.engineReady; repeat: true; onTriggered: { if (root.selectedTicket && inspector.opened) root.request("detail", {ticket: root.selectedTicket}); } }

    Connections {
        target: backend
        function onReady(json) {
            var data = JSON.parse(json); root.engineReady = true; root.projectPath = data.project;
            root.statusMessage = data.notice || "Project ready. Work stays in ticket worktrees.";
            root.request("snapshot");
        }
        function onFatal(message) { root.statusMessage = message; errorText.text = message; errorDialog.open(); }
        function onEvent(json) {
            var event = JSON.parse(json);
            if (event.kind === "ModelGenerationStarted") {
                root.setRunGenerating(event.run_id, true);
                if (event.session_id === root.selectedSession) conversationView.interrupted = false;
            } else if (event.kind === "ModelGenerationFinished") {
                root.setRunGenerating(event.run_id, false);
                if (event.session_id === root.selectedSession)
                    conversationView.interrupted = !!event.data.stopped;
            } else if (event.kind === "StepAttemptStarted") {
                refreshTimer.restart();
                if (event.ticket_id === root.selectedTicket && inspector.opened) {
                    root.selectSession(event.session_id);
                    root.request("detail", {ticket: root.selectedTicket});
                }
            } else if (["ModelTextDelta", "ModelReasoningDelta", "ModelToolDelta"].indexOf(event.kind) >= 0) {
                conversationView.appendDelta(event);
            } else if (event.kind === "DiscussionGenerationStarted") {
                root.generatingDiscussionId = event.data.discussion_id;
                if (event.session_id === discussionView.sessionId) discussionView.interrupted = false;
            } else if (event.kind === "DiscussionGenerationFinished") {
                if (root.generatingDiscussionId === event.data.discussion_id) root.generatingDiscussionId = "";
                if (event.session_id === discussionView.sessionId)
                    discussionView.interrupted = !!event.data.stopped;
            } else if (event.kind === "DiscussionDelta") {
                if (event.session_id === discussionView.sessionId) {
                    var mapped = event.data.kind === "text" ? "ModelTextDelta" : (event.data.kind === "reasoning" ? "ModelReasoningDelta" : (event.data.kind === "tool_delta" ? "ModelToolDelta" : ""));
                    if (mapped) discussionView.appendDelta({kind: mapped, session_id: event.session_id, data: {request_id: event.data.request_id, value: event.data.value}});
                }
            } else if (event.kind === "DiscussionChanged") {
                root.refreshDiscussions();
            } else {
                refreshTimer.restart();
                if (event.session_id === root.selectedSession && ["ConversationChanged", "ToolStateChanged", "StepAttemptCompleted"].indexOf(event.kind) >= 0)
                    conversationRefresh.restart();
                if (event.kind === "QuestionRequested" || event.kind === "ApprovalRequested") { root.statusMessage = "A ticket needs your input."; root.openTicket(event.ticket_id); }
                if (event.kind === "IntegrationBlocked" || event.kind === "RecoveryRequired") root.statusMessage = event.data.error || event.data.message || "Review required.";
            }
        }
        function onResponse(identity, action, json) {
            var envelope = JSON.parse(json);
            if (!envelope.ok) {
                root.statusMessage = envelope.error; errorText.text = envelope.error; errorDialog.open();
                if (action === "discuss") { root.discussionBusy = false; root.generatingDiscussionId = ""; }
                if (action === "discussion_ticket_draft") { root.ticketDraftBusy = false; root.ticketDraftRequestId = ""; }
                if (action === "conversation" && identity === root.conversationOlderRequestId) root.conversationOlderRequestId = "";
                return;
            }
            var data = envelope.data;
            if (action === "snapshot") { root.tickets = data.board; root.teams = data.teams; root.settings = data.settings; root.skills = data.skills; root.discussions = data.discussions || []; root.dark = data.settings.theme !== "light"; root.loadSettings(); }
            else if (action === "board") root.tickets = data;
            else if (action === "detail") {
                if (data.ticket.id === root.selectedTicket) {
                    var oldState = root.detail.run ? root.detail.run.state : "";
                    root.detail = data;
                    if (root.selectedSession && !(data.attempts || []).some(function(attempt) { return attempt.session_id === root.selectedSession; })) {
                        root.selectedSession = "";
                        root.transcript = [];
                        root.conversationRequestId = "";
                        root.conversationOlderRequestId = "";
                        root.conversationHasOlder = false;
                        conversationView.resetLive();
                    } else if (data.run && data.run.state !== oldState && root.selectedSession) {
                        conversationRefresh.restart();
                    }
                }
            }
            else if (action === "create") { newTicketDialog.close(); root.pendingSourceDiscussion = ""; root.pendingSourceDiscussionTitle = ""; root.pendingDraftTitle = ""; root.pendingDraftDescription = ""; root.openTicket(data.id); root.refresh(); }
            else if (action === "conversation") {
                if (data.session === root.selectedSession && identity === root.conversationOlderRequestId) {
                    root.transcript = root.mergeConversationRows(data.messages, root.transcript);
                    root.conversationHasOlder = data.has_older;
                    root.conversationOlderRequestId = "";
                } else if (data.session === root.selectedSession && identity === root.conversationRequestId) {
                    var wasEmpty = root.transcript.length === 0;
                    root.transcript = root.mergeConversationRows(root.transcript, data.messages);
                    if (wasEmpty) root.conversationHasOlder = data.has_older;
                }
            }
            else if (action === "file_list") { if (data.ticket === root.selectedTicket) root.fileList = data.files; }
            else if (action === "models") { root.models = data; modelsDialog.open(); }
            else if (action === "settings" || action === "reset_settings" || action === "save_team") { root.request("snapshot"); root.statusMessage = "Configuration saved."; advancedDialog.close(); }
            else if (action === "preview_checkpoint") { root.pendingRestore = data; restoreText.text = JSON.stringify(data, null, 2); restoreDialog.open(); }
            else if (action === "recovery") root.recovery = data;
            else if (action === "job") { root.selectedJob = data.id; jobOutput.text = data.command + "\nState: " + data.status + "; exit: " + data.exit_code + "\n\n" + data.output + (data.partial ? "\n[More output remains; use the CLI job --offset command.]" : ""); }
            else if (action === "discussion_list") {
                root.discussions = data;
                if (root.selectedDiscussion && root.selectedDiscussion.id) {
                    var current = data.find(function(item) { return item.id === root.selectedDiscussion.id; });
                    if (current) root.selectedDiscussion = current;
                    else root.selectDiscussion(null);
                }
                if ((!root.selectedDiscussion || !root.selectedDiscussion.id) && data.length > 0) root.selectDiscussion(data[0]);
            }
            else if (action === "discussion_create") { root.selectDiscussion(data); root.refreshDiscussions(); }
            else if (action === "discussion_ticket_draft") {
                if (identity === root.ticketDraftRequestId) {
                    root.ticketDraftBusy = false;
                    root.ticketDraftRequestId = "";
                    root.prepareNewTicket(data.discussion.id, data.discussion.title, data.title, data.description);
                }
            }
            else if (action === "discussion_rename") { if (root.selectedDiscussion.id === data.id) root.selectedDiscussion = data; root.refreshDiscussions(); discussionRenameDialog.close(); }
            else if (action === "discussion_archive") {
                if (root.selectedDiscussion.id === data.id) {
                    if (data.archived && !showArchivedDiscussions.checked) root.selectDiscussion(null);
                    else root.selectedDiscussion = data;
                }
                root.refreshDiscussions();
            }
            else if (action === "discuss") {
                root.discussionBusy = false;
                if (data.discussion && root.generatingDiscussionId === data.discussion.id) root.generatingDiscussionId = "";
                if (data.discussion && root.selectedDiscussion.id === data.discussion.id) {
                    root.selectedDiscussion = data.discussion;
                    root.discussionHistoryRequestId = root.request("discussion_history", {discussion: data.discussion.id});
                }
                root.refreshDiscussions();
            }
            else if (action === "discussion_history") {
                if (identity === root.discussionHistoryRequestId && root.selectedDiscussion.id === data.discussion.id) {
                    root.selectedDiscussion = data.discussion;
                    root.discussionTranscript = data.messages;
                    discussionView.resetLive();
                }
            }
            else if (action === "backup" || action === "export") { root.statusMessage = "Saved " + data.path; pathDialog.close(); }
            else if (["code", "diff", "compare_file", "integration_diff", "request"].indexOf(action) < 0) { refreshTimer.restart(); root.statusMessage = "Action recorded: " + action.replace(/_/g, " "); }
        }
    }

    header: Rectangle {
        height: 66; color: root.panel
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: root.borderColor }
        RowLayout {
            anchors.fill: parent; anchors.leftMargin: 24; anchors.rightMargin: 24; spacing: 16
            Rectangle { width: 30; height: 30; radius: 8; color: "#8ad8bd"; Text { anchors.centerIn: parent; text: "A"; color: "#193b31"; font.pixelSize: 20; font.bold: true } }
            Text { text: "AGILECODE"; color: root.ink; font.pixelSize: 16; font.weight: Font.DemiBold; font.letterSpacing: 2 }
            Rectangle { width: 1; height: 24; color: root.borderColor }
            Text { text: root.projectPath; color: root.muted; font.pixelSize: 12; elide: Text.ElideMiddle; Layout.fillWidth: true }
            Rectangle { width: 7; height: 7; radius: 4; color: root.engineReady ? "#8ad8bd" : "#c8a578" }
            Text { text: root.engineReady ? "ENGINE READY" : "STARTING"; font.pixelSize: 10; color: root.muted; font.letterSpacing: 1 }
            Text { text: "0.1.0"; font.pixelSize: 11; color: root.muted }
        }
    }
    footer: Rectangle {
        height: 38; color: root.panel
        Rectangle { width: parent.width; height: 1; color: root.borderColor }
        Text { anchors.fill: parent; anchors.leftMargin: 24; anchors.rightMargin: 24; verticalAlignment: Text.AlignVCenter; text: root.statusMessage; font.pixelSize: 12; color: root.muted; elide: Text.ElideRight }
    }
    RowLayout {
        anchors.fill: parent; spacing: 0
        Rectangle {
            Layout.fillHeight: true; Layout.preferredWidth: 204; color: root.panel
            Rectangle { anchors.right: parent.right; height: parent.height; width: 1; color: root.borderColor }
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 17; spacing: 9
                Text { text: "WORKSPACE"; color: root.muted; font.pixelSize: 10; font.letterSpacing: 1.5; Layout.topMargin: 10; Layout.bottomMargin: 10 }
                Repeater {
                    model: ["Ticket board", "Project discussions", "Teams & processes", "Settings & tools", "Recovery"]
                    delegate: ActionButton {
                        required property string modelData
                        required property int index
                        text: modelData; dark: root.dark; primary: root.currentPage === index
                        Layout.fillWidth: true
                        onClicked: { root.currentPage = index; if (index === 4) root.request("recovery"); if (index === 1) root.refreshDiscussions(); }
                    }
                }
                Item { Layout.fillHeight: true }
                Rectangle {
                    Layout.fillWidth: true; height: 113; radius: 8; color: root.dark ? "#20342f" : "#e2f2e9"
                    Column {
                        anchors.fill: parent; anchors.margins: 12; spacing: 7
                        Text { text: "LOCAL MODEL"; font.pixelSize: 10; font.letterSpacing: 1; color: root.dark ? "#9bddc3" : "#24694e" }
                        Text { width: parent.width; text: root.settings.profiles ? (root.settings.profiles[root.settings.active_profile].model || "Use server model") : "Not connected"; wrapMode: Text.WordWrap; maximumLineCount: 2; font.pixelSize: 12; color: root.ink }
                        Text { text: "Independent ticket worktrees"; font.pixelSize: 10; color: root.muted }
                    }
                }
            }
        }
        StackLayout {
            currentIndex: root.currentPage; Layout.fillWidth: true; Layout.fillHeight: true
            Item {
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 25; spacing: 20
                    RowLayout {
                        Layout.fillWidth: true
                        Column {
                            spacing: 5
                            Text { text: "Ticket board"; color: root.ink; font.pixelSize: 27; font.weight: Font.DemiBold }
                            Text { text: "Plan, execute, verify, and integrate — one ticket at a time or in parallel."; color: root.muted; font.pixelSize: 12 }
                        }
                        Item { Layout.fillWidth: true }
                        ActionButton { text: "Refresh"; dark: root.dark; onClicked: root.refresh() }
                        ActionButton { text: "+ New ticket"; primary: true; dark: root.dark; enabled: root.engineReady; onClicked: root.prepareNewTicket("", "") }
                    }
                    RowLayout {
                        Input { id: searchField; Layout.fillWidth: true; dark: root.dark; placeholderText: "Filter tickets by title or key…" }
                        CheckBox { id: showArchived; text: "Archived"; onToggled: root.refresh() }
                        Text { text: root.tickets.length + " tickets"; color: root.muted; font.pixelSize: 12 }
                    }
                    RowLayout {
                        Layout.fillWidth: true; Layout.fillHeight: true; spacing: 14
                        Repeater {
                            model: root.states
                            delegate: Rectangle {
                                id: lane
                                required property string modelData
                                required property int index
                                Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumWidth: 160
                                color: root.dark ? "#14212c" : "#eaf0f5"; radius: 10
                                border.color: drop.containsDrag ? "#8ad8bd" : root.borderColor
                                ColumnLayout {
                                    anchors.fill: parent; anchors.margins: 12; spacing: 13
                                    RowLayout {
                                        Layout.fillWidth: true; Layout.topMargin: 3; Layout.bottomMargin: 4
                                        Rectangle { width: 7; height: 7; radius: 4; color: ["#90a5b9", "#d6b484", "#91bbdc", "#8ad8bd"][lane.index] }
                                        Text { text: lane.modelData; color: root.ink; font.pixelSize: 13; font.weight: Font.DemiBold }
                                        Item { Layout.fillWidth: true }
                                        Text { text: root.filtered(lane.modelData).length; color: root.muted; font.pixelSize: 12 }
                                    }
                                    ListView {
                                        id: ticketList
                                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                                        model: root.filtered(lane.modelData)
                                        boundsBehavior: Flickable.StopAtBounds
                                        ScrollBar.vertical: ScrollBar {}
                                        delegate: TicketCard {
                                            required property var modelData
                                            width: ticketList.width
                                            ticket: modelData; dark: root.dark; dragLayer: root.contentItem
                                            onSelected: function(ticketId) { root.openTicket(ticketId); }
                                        }
                                        Text { anchors.centerIn: parent; visible: ticketList.count === 0; text: "No tickets"; color: root.muted; font.pixelSize: 12 }
                                    }
                                }
                                DropArea {
                                    id: drop; anchors.fill: parent; keys: ["agilecode-ticket"]
                                    onDropped: function(event) {
                                        var ordered = root.filtered(lane.modelData).filter(function(t) { return t.id !== event.source.ticketId; });
                                        var point = ticketList.mapFromItem(drop, event.x, event.y);
                                        var at = Math.max(0, Math.min(ordered.length, Math.floor((point.y + ticketList.contentY) / 147)));
                                        var position = ordered.length === 0 ? 0 : (at === 0 ? ordered[0].position - 1 : (at === ordered.length ? ordered[at-1].position + 1 : (ordered[at-1].position + ordered[at].position) / 2));
                                        root.request("move", {ticket: event.source.ticketId, state: lane.modelData, position: position, revision: event.source.ticketRevision}); event.acceptProposedAction();
                                    }
                                }
                            }
                        }
                    }
                }
            }
            Item {
                RowLayout {
                    anchors.fill: parent; anchors.margins: 24; spacing: 18
                    ColumnLayout {
                        Layout.preferredWidth: 285; Layout.minimumWidth: 240; Layout.fillHeight: true; spacing: 10
                        RowLayout {
                            Layout.fillWidth: true
                            Text { text: "Discussions"; color: root.ink; font.pixelSize: 24; font.weight: Font.DemiBold }
                            Item { Layout.fillWidth: true }
                            ActionButton { text: "+ New chat"; primary: true; dark: root.dark; onClicked: root.request("discussion_create", {}) }
                        }
                        Text { text: "Separate read-only chats about this project."; color: root.muted; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                        CheckBox { id: showArchivedDiscussions; text: "Show archived"; onToggled: root.refreshDiscussions() }
                        ListView {
                            id: discussionList
                            Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 2
                            model: root.discussions
                            ScrollBar.vertical: ScrollBar {}
                            delegate: ItemDelegate {
                                required property var modelData
                                width: ListView.view.width; height: 58
                                highlighted: root.selectedDiscussion && root.selectedDiscussion.id === modelData.id
                                contentItem: Column {
                                    spacing: 4
                                    Text { width: parent.width; text: modelData.title; color: root.ink; elide: Text.ElideRight; font.pixelSize: 13; font.weight: Font.DemiBold }
                                    Text { text: (modelData.archived ? "Archived · " : "") + modelData.updated.slice(0, 16).replace("T", " "); color: root.muted; font.pixelSize: 10 }
                                }
                                background: Rectangle { color: parent.highlighted ? (root.dark ? "#253b49" : "#e3edf5") : (parent.hovered ? (root.dark ? "#1d303e" : "#eef3f7") : "transparent"); radius: 6 }
                                onClicked: root.selectDiscussion(modelData)
                            }
                            Text { anchors.centerIn: parent; visible: parent.count === 0; text: "No discussions yet."; color: root.muted; font.pixelSize: 12 }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            ActionButton { text: "Rename"; dark: root.dark; enabled: !!root.selectedDiscussion.id; onClicked: { discussionRenameText.text = root.selectedDiscussion.title || ""; discussionRenameDialog.open(); } }
                            ActionButton { text: root.selectedDiscussion.archived ? "Restore" : "Archive"; dark: root.dark; enabled: !!root.selectedDiscussion.id && !root.discussionBusy; onClicked: root.request("discussion_archive", {discussion: root.selectedDiscussion.id, archived: !root.selectedDiscussion.archived}) }
                        }
                    }
                    Rectangle { Layout.fillHeight: true; width: 1; color: root.borderColor }
                    ColumnLayout {
                        Layout.fillWidth: true; Layout.fillHeight: true; spacing: 12
                        RowLayout {
                            Layout.fillWidth: true
                            ColumnLayout {
                                Layout.fillWidth: true; spacing: 3
                                Text { text: root.selectedDiscussion.id ? root.selectedDiscussion.title : "Project discussion"; color: root.ink; font.pixelSize: 25; font.weight: Font.DemiBold; elide: Text.ElideRight; Layout.fillWidth: true }
                                Text { text: root.selectedDiscussion.id ? (root.selectedDiscussion.archived ? "Archived discussion · restore it to continue chatting." : "Read-only investigation of the original project.") : "Open a chat or start a new one to discuss the project before creating a ticket."; color: root.muted; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                            }
                            ActionButton {
                                text: root.ticketDraftBusy ? "Drafting ticket…" : "Create ticket"
                                dark: root.dark
                                enabled: !!root.selectedDiscussion.id && !root.discussionBusy && !root.ticketDraftBusy
                                onClicked: {
                                    root.ticketDraftBusy = true;
                                    root.ticketDraftRequestId = root.request("discussion_ticket_draft", {discussion: root.selectedDiscussion.id});
                                }
                            }
                        }
                        Item {
                            Layout.fillWidth: true; Layout.fillHeight: true
                            Text { anchors.centerIn: parent; visible: !root.selectedDiscussion.id; text: "Select a discussion or start a new chat."; color: root.muted; font.pixelSize: 13 }
                            ConversationView {
                                id: discussionView
                                objectName: "projectDiscussionView"
                                anchors.fill: parent
                                visible: !!root.selectedDiscussion.id
                                sessionId: root.selectedDiscussion.session_id || ""
                                messages: root.discussionTranscript.filter(function(record) { return record.body && record.body.role !== "system"; })
                                dark: root.dark
                                reasoningExpandedByDefault: !!root.settings.open_reasoning_by_default
                                assistantName: "Assistant"
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            visible: !!root.selectedDiscussion.id
                            spacing: 8
                            ScrollView {
                                id: discussionComposer
                                Layout.fillWidth: true
                                Layout.preferredHeight: Math.min(132, Math.max(42, discussionInput.contentHeight + 18))
                                contentWidth: availableWidth
                                clip: true
                                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                                TextArea {
                                    id: discussionInput
                                    objectName: "discussionInput"
                                    width: discussionComposer.availableWidth
                                    enabled: !root.selectedDiscussion.archived
                                    placeholderText: root.selectedDiscussion.archived ? "Restore this discussion to continue…" : "Ask about your codebase…"
                                    wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
                                    selectByMouse: true
                                    padding: 9
                                    font.pixelSize: 14
                                    color: root.ink
                                    placeholderTextColor: root.muted
                                    selectionColor: "#396858"
                                    background: Rectangle {
                                        color: root.dark ? "#101c27" : "white"
                                        radius: 7
                                        border.color: discussionInput.activeFocus ? "#8ad8bd" : root.borderColor
                                    }
                                    Keys.onReturnPressed: function(event) {
                                        if (!(event.modifiers & Qt.ShiftModifier) && discussionSend.enabled) {
                                            discussionSend.clicked();
                                            event.accepted = true;
                                        } else {
                                            event.accepted = false;
                                        }
                                    }
                                }
                            }
                            ActionButton {
                                id: discussionSend
                                objectName: "discussionSend"
                                text: "Send"
                                primary: true
                                dark: root.dark
                                visible: !root.discussionBusy
                                enabled: !root.discussionBusy && !root.selectedDiscussion.archived && discussionInput.text.trim().length > 0
                                onClicked: {
                                    var question = discussionInput.text.trim();
                                    var optimistic = root.discussionTranscript.slice();
                                    optimistic.push({id: "pending-discussion-" + Date.now(), seq: optimistic.length + 1, body: {role: "user", content: question}, partial: false});
                                    root.discussionTranscript = optimistic;
                                    discussionView.resetLive();
                                    root.discussionBusy = true;
                                    root.generatingDiscussionId = root.selectedDiscussion.id;
                                    root.request("discuss", {discussion: root.selectedDiscussion.id, text: question});
                                    discussionInput.clear();
                                }
                            }
                            ActionButton {
                                id: discussionStop
                                objectName: "discussionStop"
                                text: "Stop"
                                danger: true
                                dark: root.dark
                                visible: root.discussionBusy && root.generatingDiscussionId === root.selectedDiscussion.id
                                enabled: !!root.selectedDiscussion.id
                                onClicked: root.request("discussion_stop", {discussion: root.selectedDiscussion.id})
                            }
                        }
                    }
                }
            }
            TeamManager {
                id: teamManager
                Layout.fillWidth: true
                Layout.fillHeight: true
                teams: root.validTeams()
                dark: root.dark
                ink: root.ink
                muted: root.muted
                panel: root.panel
                borderColor: root.borderColor
                onSaveRequested: function(team, scope) {
                    root.request("save_team", {team: team, scope: scope});
                }
            }
            ScrollView {
                clip: true
                ColumnLayout {
                    width: parent.width - 50; x: 25; y: 24; spacing: 14
                    Text { text: "Settings & tools"; color: root.ink; font.pixelSize: 27; font.weight: Font.DemiBold }
                    Text { text: "Connection, autonomy, integration and defaults. Advanced settings include MCP and per-tool policy."; color: root.muted; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    RowLayout { Text { text: "Save changes to"; color: root.muted } ComboBox { id: settingsScope; model: ["This project", "Global default"] } Item { Layout.fillWidth: true } ActionButton { text: "Advanced JSON"; dark: root.dark; onClicked: { advancedText.text = JSON.stringify(root.settings, null, 2); advancedDialog.open(); } } }
                    Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }
                    Text { text: "Local model connection"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                    Text { text: root.settings.active_profile ? "Active profile: " + root.settings.active_profile + "  (use Advanced JSON to add or select profiles)" : ""; color: root.muted; font.pixelSize: 11 }
                    GridLayout {
                        columns: 2; Layout.fillWidth: true; rowSpacing: 11; columnSpacing: 18
                        Text { text: "Base URL"; color: root.muted; Layout.preferredWidth: 150 }
                        Input { id: endpointField; dark: root.dark; Layout.fillWidth: true; placeholderText: "http://127.0.0.1:1234/v1" }
                        Text { text: "Model ID"; color: root.muted }
                        RowLayout { Layout.fillWidth: true; Input { id: modelField; dark: root.dark; Layout.fillWidth: true; placeholderText: "Blank = use the single model listed by the server" } ActionButton { text: "List models"; dark: root.dark; onClicked: root.request("models") } }
                        Text { text: "API key environment variable"; color: root.muted; wrapMode: Text.WordWrap; Layout.preferredWidth: 150 }
                        Input { id: keyField; dark: root.dark; Layout.fillWidth: true; placeholderText: "Optional variable name, not the key value" }
                        Text { text: "Model timeout (seconds)"; color: root.muted }
                        Input { id: modelTimeoutField; dark: root.dark; Layout.fillWidth: true; inputMethodHints: Qt.ImhFormattedNumbersOnly; placeholderText: "Blank = no timeout" }
                    }
                    Text { text: "Save a changed endpoint before listing models. AgileCode neither loads models nor schedules provider inference."; color: root.muted; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                    Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor; Layout.topMargin: 6 }
                    Text { text: "Behavior"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                    Switch { id: autonomySwitch; text: "Full tool autonomy (explicit deny/ask rules still apply)" }
                    Switch { id: mergeSwitch; text: "Merge on completion; otherwise hold the candidate for inspection" }
                    Switch { id: reasoningOpenSwitch; text: "Open reasoning blocks by default" }
                    Switch { id: lightSwitch; text: "Light appearance" }
                    GridLayout {
                        columns: 2; Layout.fillWidth: true; rowSpacing: 11; columnSpacing: 18
                        Text { text: "Correction attempt limit"; color: root.muted; Layout.preferredWidth: 150 }
                        Input { id: correctionLimitField; dark: root.dark; Layout.fillWidth: true; inputMethodHints: Qt.ImhDigitsOnly; placeholderText: "10" }
                        Text { text: "Command timeout (seconds)"; color: root.muted }
                        Input { id: shellTimeoutField; dark: root.dark; Layout.fillWidth: true; inputMethodHints: Qt.ImhFormattedNumbersOnly; placeholderText: "Blank = no timeout" }
                    }
                    Text { text: "Command rules — first matching pattern wins"; color: root.muted; font.pixelSize: 12 }
                    Repeater {
                        model: root.commandRulesDraft
                        delegate: RowLayout {
                            required property var modelData
                            required property int index
                            Layout.fillWidth: true
                            Input {
                                dark: root.dark
                                Layout.fillWidth: true
                                text: modelData.pattern
                                placeholderText: "e.g. pip install *"
                                onEditingFinished: root.updateCommandRule(index, "pattern", text.trim())
                            }
                            ComboBox {
                                property var actions: ["allow", "ask", "deny"]
                                model: actions
                                currentIndex: Math.max(0, actions.indexOf(modelData.action))
                                onActivated: root.updateCommandRule(index, "action", actions[currentIndex])
                            }
                            ActionButton { text: "Remove"; dark: root.dark; onClicked: root.removeCommandRule(index) }
                        }
                    }
                    ActionButton { text: "+ Command rule"; dark: root.dark; onClicked: root.addCommandRule() }
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: "Default team"; color: root.muted; Layout.preferredWidth: 150 }
                        ComboBox { id: defaultTeam; Layout.fillWidth: true; model: root.validTeams().map(function(t) { return t.team.name + " / " + t.scope; }) }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: "Default process"; color: root.muted; Layout.preferredWidth: 150 }
                        ComboBox { id: defaultProcess; Layout.fillWidth: true; model: defaultTeam.currentIndex >= 0 && root.validTeams()[defaultTeam.currentIndex] ? root.validTeams()[defaultTeam.currentIndex].team.processes.map(function(p) { return p.name; }) : [] }
                    }
                    Text { text: "Required verification commands — one per line"; color: root.muted; font.pixelSize: 12 }
                    ScrollView { Layout.fillWidth: true; Layout.preferredHeight: 100; TextArea { id: verifyField; color: root.ink; font.family: "monospace"; selectByMouse: true; placeholderText: "python -m pytest -q"; background: Rectangle { color: root.panel; radius: 7; border.color: root.borderColor } } }
                    RowLayout { ActionButton { text: "Save settings"; primary: true; dark: root.dark; onClicked: root.saveBasicSettings() } ActionButton { text: "Use global defaults"; dark: root.dark; onClicked: root.request("reset_settings") } ActionButton { text: "Create backup…"; dark: root.dark; onClicked: { pathDialog.operation = "backup"; pathField.text = root.projectPath + "/../AgileCode-backup.zip"; pathDialog.open(); } } }
                    Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor; Layout.topMargin: 7 }
                    Text { text: "Discovered skills"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                    Text { text: "Enable skill names on team members (or * for all). Skill discovery errors stay visible; loading a skill never executes it."; color: root.muted; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    Repeater {
                        model: root.skills
                        delegate: Text { required property var modelData; Layout.fillWidth: true; text: modelData.error ? "ERROR · " + modelData.error : (modelData.id + " · " + (modelData.source || modelData.scope || "") + "\n" + markdownPresentation.plainText(modelData.description || "")); wrapMode: Text.WordWrap; color: modelData.error ? "#d6a385" : root.muted; font.pixelSize: 12 }
                    }
                    Item { Layout.preferredHeight: 40 }
                }
            }
            Item {
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 25; spacing: 14
                    RowLayout { Text { text: "Recovery"; color: root.ink; font.pixelSize: 27; font.weight: Font.DemiBold } Item { Layout.fillWidth: true } ActionButton { text: "Refresh"; dark: root.dark; onClicked: root.request("recovery") } }
                    Text { text: "Unknown outcomes are not replayed automatically. Inspect the actual code, jobs and external service before reconciling."; color: root.muted; font.pixelSize: 13; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                    ListView {
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 8
                        model: root.recovery.tools.map(function(t) { return {kind: "tool", item: t}; }).concat(root.recovery.jobs.map(function(j) { return {kind: "job", item: j}; }))
                        delegate: Rectangle {
                            required property var modelData
                            width: ListView.view.width; height: 94; color: root.panel; radius: 8; border.color: root.borderColor
                            RowLayout {
                                anchors.fill: parent; anchors.margins: 14
                                ColumnLayout { Layout.fillWidth: true; Text { text: modelData.kind.toUpperCase() + " · " + (modelData.item.name || modelData.item.command); color: root.ink; font.pixelSize: 14; Layout.fillWidth: true; elide: Text.ElideRight } Text { text: modelData.item.id; color: root.muted; font.pixelSize: 11 } }
                                ActionButton { text: "Reconcile…"; dark: root.dark; onClicked: { root.pendingReconcile = modelData; reconcileDialog.open(); } }
                            }
                        }
                        Text { anchors.centerIn: parent; visible: parent.count === 0; text: "No unknown tool or job outcomes."; color: root.muted }
                    }
                    Text { text: root.recovery.operations.length + " prepared administrative operations. Git/checkpoint recovery reconciles identifiable outcomes at startup."; color: root.muted; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                }
            }
        }
    }

    Drawer {
        id: inspector
        objectName: "ticketInspector"
        edge: Qt.RightEdge; width: Math.min(root.width - 160, 780); height: root.height - root.header.height - root.footer.height; y: root.header.height
        modal: false; interactive: false
        background: Rectangle { color: root.panel; border.color: root.borderColor }
        contentItem: ColumnLayout {
            spacing: 12
            RowLayout {
                Layout.fillWidth: true; Layout.margins: 18; Layout.bottomMargin: 0
                ColumnLayout { Layout.fillWidth: true; Text { text: root.detail.ticket ? root.detail.ticket.key + " · " + root.detail.ticket.state : "Ticket"; color: root.muted; font.pixelSize: 12 } Text { text: root.detail.ticket ? root.detail.ticket.title : "Loading…"; color: root.ink; font.pixelSize: 21; font.weight: Font.DemiBold; wrapMode: Text.WordWrap; Layout.fillWidth: true } }
                ActionButton { text: "Close"; dark: root.dark; onClicked: inspector.close() }
            }
            Flow {
                Layout.fillWidth: true; Layout.leftMargin: 18; Layout.rightMargin: 18; Layout.preferredHeight: childrenRect.height; spacing: 7
                ActionButton { text: "Ready"; dark: root.dark; visible: root.detail.ticket && root.detail.ticket.state === "Backlog"; onClicked: root.request("move", {ticket: root.selectedTicket, state: "Ready"}) }
                ActionButton { text: "Start"; primary: true; dark: root.dark; visible: root.detail.ticket && root.detail.ticket.state === "Ready"; onClicked: root.request("start", {ticket: root.selectedTicket}) }
                ActionButton {
                    objectName: "ticketResumeExecution"
                    text: "Resume"
                    primary: true
                    dark: root.dark
                    visible: !!root.detail.execution && !!root.detail.execution.can_resume
                    onClicked: root.request("resume_execution", {ticket: root.selectedTicket})
                }
                ActionButton { text: "Pause"; dark: root.dark; visible: !!root.detail.execution && !!root.detail.execution.can_pause; onClicked: root.request("pause", {ticket: root.selectedTicket}) }
                ActionButton { text: "Cancel run"; danger: true; dark: root.dark; visible: !!root.detail.execution && !!root.detail.execution.run_id; onClicked: cancelDialog.open() }
                ActionButton { text: "Ticket diff"; dark: root.dark; enabled: !!root.detail.run; onClicked: root.request("diff", {ticket: root.selectedTicket}) }
                ActionButton { text: "Export"; dark: root.dark; onClicked: { pathDialog.operation = "export"; pathField.text = root.projectPath + "/../" + (root.detail.ticket ? root.detail.ticket.key : "ticket") + ".json"; pathDialog.open(); } }
            }
            Repeater {
                model: root.detail.waits || []
                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true; Layout.leftMargin: 18; Layout.rightMargin: 18; implicitHeight: waitColumn.implicitHeight + 22
                    color: root.dark ? "#313126" : "#fff4dc"; radius: 8; border.color: "#907849"
                    ColumnLayout {
                        id: waitColumn; anchors.fill: parent; anchors.margins: 11
                        Text { text: modelData.kind === "question" ? "QUESTION — SESSION PAUSED" : "TOOL APPROVAL"; color: root.dark ? "#e1c994" : "#705923"; font.pixelSize: 10; font.weight: Font.Bold }
                        Text { text: modelData.kind === "question" ? markdownPresentation.plainText(modelData.prompt).slice(0, 2200) : modelData.prompt.slice(0, 2200); color: root.ink; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true; font.pixelSize: 12; maximumLineCount: 10; elide: Text.ElideRight }
                        RowLayout {
                            ActionButton { text: "Answer…"; primary: true; dark: root.dark; visible: modelData.kind === "question"; onClicked: { root.pendingQuestion = modelData.id; answerText.clear(); questionText.sourceText = modelData.prompt; answerDialog.open(); } }
                            ActionButton { text: "Approve"; primary: true; dark: root.dark; visible: modelData.kind === "approval"; onClicked: root.request("answer", {id: modelData.id, answer: "approve"}) }
                            ActionButton { text: "Deny"; dark: root.dark; visible: modelData.kind === "approval"; onClicked: root.request("answer", {id: modelData.id, answer: "deny"}) }
                        }
                    }
                }
            }
            TabBar {
                id: inspectTabs
                objectName: "inspectorTabs"
                Layout.fillWidth: true
                Repeater { model: ["Overview", "Steps", "Conversation"]; delegate: TabButton { required property string modelData; text: modelData; font.pixelSize: 12 } }
            }
            StackLayout {
                currentIndex: inspectTabs.currentIndex; Layout.fillWidth: true; Layout.fillHeight: true; Layout.leftMargin: 18; Layout.rightMargin: 18; Layout.bottomMargin: 14
                ScrollView {
                    id: ticketOverviewScroll
                    objectName: "ticketOverviewScroll"
                    clip: true
                    contentWidth: availableWidth
                    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    ColumnLayout {
                        width: ticketOverviewScroll.availableWidth
                        spacing: 14
                        RowLayout {
                            Layout.fillWidth: true
                            Text { text: "TICKET DESCRIPTION"; color: root.muted; font.pixelSize: 10; font.letterSpacing: 1 }
                            Item { Layout.fillWidth: true }
                            ActionButton {
                                objectName: "ticketDescriptionToggle"
                                text: root.ticketDescriptionExpanded ? "Collapse" : "Expand"
                                dark: root.dark
                                visible: !!root.detail.ticket && root.detail.ticket.description.length > 0
                                onClicked: root.ticketDescriptionExpanded = !root.ticketDescriptionExpanded
                            }
                        }
                        Text {
                            objectName: "ticketDescriptionPreview"
                            visible: !root.ticketDescriptionExpanded
                            text: root.detail.ticket ? root.detail.ticket.description : ""
                            textFormat: Text.MarkdownText
                            color: root.ink
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                            font.pixelSize: 14
                            maximumLineCount: 6
                            elide: Text.ElideRight
                            onLinkActivated: function(link) { Qt.openUrlExternally(link); }
                        }
                        ScrollView {
                            id: ticketDescriptionScroll
                            objectName: "ticketDescriptionScroll"
                            visible: root.ticketDescriptionExpanded
                            Layout.fillWidth: true
                            Layout.preferredHeight: 280
                            contentWidth: availableWidth
                            clip: true
                            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                            TextArea {
                                id: ticketDescriptionText
                                objectName: "ticketDescriptionText"
                                width: ticketDescriptionScroll.availableWidth
                                text: root.detail.ticket ? root.detail.ticket.description : ""
                                textFormat: TextEdit.MarkdownText
                                readOnly: true
                                selectByMouse: true
                                wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
                                color: root.ink
                                font.pixelSize: 14
                                padding: 8
                                onLinkActivated: function(link) { Qt.openUrlExternally(link); }
                                background: Rectangle {
                                    color: root.dark ? "#101923" : "#f7f9fb"
                                    radius: 7
                                    border.color: root.borderColor
                                }
                                ChatDocumentStyle {
                                    document: ticketDescriptionText.textDocument
                                    dark: root.dark
                                }
                            }
                        }
                        RowLayout {
                            visible: !!root.detail.source_discussion
                            Layout.fillWidth: true
                            Text { text: root.detail.source_discussion ? "SOURCE DISCUSSION · " + root.detail.source_discussion.title : ""; color: root.muted; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                            ActionButton { text: "Open discussion"; dark: root.dark; onClicked: { var source = root.detail.source_discussion; inspector.close(); root.currentPage = 1; if (source.archived) showArchivedDiscussions.checked = true; root.selectDiscussion(source); root.refreshDiscussions(); } }
                        }
                        Text { text: root.detail.run ? "Execution: " + root.detail.run.state + "\n" + (root.detail.run.error || "") + "\n\nBranch: " + root.detail.run.branch + "\nBase: " + root.detail.run.base_oid + "\nWorktree: " + root.detail.run.workspace : "No run yet."; color: root.muted; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true; font.pixelSize: 12 }
                        RowLayout {
                            ActionButton { text: "Edit ticket…"; dark: root.dark; onClicked: { editTitle.text = root.detail.ticket.title; editDescription.text = markdownPresentation.plainText(root.detail.ticket.description); editTicketDialog.open(); } }
                            ActionButton { text: "Archive"; dark: root.dark; enabled: root.detail.ticket && root.detail.ticket.state !== "In Progress"; onClicked: root.request("archive", {ticket: root.selectedTicket}) }
                            ActionButton { text: "Reopen"; dark: root.dark; visible: root.detail.ticket && root.detail.ticket.state === "Done"; onClicked: root.request("move", {ticket: root.selectedTicket, state: "Backlog"}) }
                        }
                        Text { text: "DEPENDENCIES"; color: root.muted; font.pixelSize: 10; font.letterSpacing: 1 }
                        Repeater { model: root.detail.dependencies || []; delegate: RowLayout { required property var modelData; Text { text: modelData.key + " · " + modelData.title; color: root.ink; Layout.fillWidth: true; elide: Text.ElideRight } ActionButton { text: "Remove"; dark: root.dark; onClicked: root.request("remove_dependency", {ticket: root.selectedTicket, depends_on: modelData.id}) } } }
                        RowLayout { Input { id: dependencyField; dark: root.dark; Layout.fillWidth: true; placeholderText: "Dependency ticket key, e.g. T-002" } ActionButton { text: "Add"; dark: root.dark; onClicked: root.request("dependency", {ticket: root.selectedTicket, depends_on: dependencyField.text}) } }
                    }
                }
                ColumnLayout {
                    ListView {
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 9
                        model: root.detail.attempts || []
                        delegate: Rectangle {
                            required property var modelData
                            width: ListView.view.width; height: 103; radius: 8; color: root.dark ? "#1d2e3c" : "#edf3f7"; border.color: root.borderColor
                            ColumnLayout {
                                anchors.fill: parent; anchors.margins: 12; spacing: 5
                                RowLayout { Layout.fillWidth: true; Text { text: modelData.step_id.replace(/_/g," ") + " · attempt " + modelData.number; color: root.ink; font.pixelSize: 13; font.weight: Font.DemiBold } Item { Layout.fillWidth: true } Text { text: modelData.result ? modelData.result.status : modelData.status; color: modelData.result && modelData.result.status !== "PASS" ? "#ddb08a" : "#8ad8bd"; font.pixelSize: 11 } }
                                Text { text: modelData.member.name + " / " + modelData.model.model + " · " + modelData.phase; color: root.muted; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                                Text { text: modelData.result ? markdownPresentation.plainText(modelData.result.summary) : "Open session to inspect progress and prompts."; color: root.muted; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight }
                            }
                            MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.selectSession(modelData.session_id) }
                        }
                    }
                    Text { text: "Click an attempt to inspect its session. Earlier attempts remain intact after corrections."; color: root.muted; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                }
                ColumnLayout {
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: root.selectedSession ? root.selectedSession.slice(0, 12) : "Select a step first"; color: root.muted; font.pixelSize: 11 }
                        Item { Layout.fillWidth: true }
                        ActionButton {
                            objectName: "ticketStopGeneration"
                            text: "Stop generation"
                            danger: true
                            dark: root.dark
                            visible: !!root.detail.execution && !!root.detail.execution.can_pause
                            enabled: !!root.generatingRuns[root.detail.run ? root.detail.run.id : ""] || conversationView.hasLiveMessage
                            onClicked: root.request("stop_generation", {ticket: root.selectedTicket})
                        }
                        ActionButton {
                            objectName: "conversationBottom"
                            text: "Bottom"
                            dark: root.dark
                            enabled: root.selectedSession.length > 0
                            onClicked: conversationView.jumpToTail()
                        }
                    }
                    ActionButton {
                        text: "Load 200 older messages"
                        dark: root.dark
                        visible: root.selectedSession.length > 0 && root.conversationHasOlder
                        enabled: root.conversationOlderRequestId.length === 0
                        onClicked: root.loadOlderConversation()
                    }
                    ConversationView {
                        id: conversationView
                        Layout.fillWidth: true; Layout.fillHeight: true
                        sessionId: root.selectedSession
                        messages: root.transcript
                        dark: root.dark
                        reasoningExpandedByDefault: !!root.settings.open_reasoning_by_default
                        assistantName: {
                            var attempt = (root.detail.attempts || []).find(function(item) { return item.session_id === root.selectedSession; });
                            return attempt && attempt.member ? attempt.member.name : "Assistant";
                        }
                        interrupted: !!root.detail.run && ["paused", "failed", "cancelled", "needs_review"].indexOf(root.detail.run.state) >= 0
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        ScrollView {
                            id: ticketCorrectionComposer
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.min(110, Math.max(42, ticketCorrectionInput.contentHeight + 18))
                            contentWidth: availableWidth
                            clip: true
                            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                            TextArea {
                                id: ticketCorrectionInput
                                objectName: "ticketCorrectionInput"
                                width: ticketCorrectionComposer.availableWidth
                                placeholderText: "Correct or redirect the current ticket work…"
                                wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
                                selectByMouse: true
                                padding: 9
                                font.pixelSize: 13
                                color: root.ink
                                placeholderTextColor: root.muted
                                selectionColor: "#396858"
                                background: Rectangle {
                                    color: root.dark ? "#101c27" : "white"
                                    radius: 7
                                    border.color: ticketCorrectionInput.activeFocus ? "#8ad8bd" : root.borderColor
                                }
                                Keys.onReturnPressed: function(event) {
                                    if (!(event.modifiers & Qt.ShiftModifier) && ticketCorrectionSend.enabled) {
                                        ticketCorrectionSend.clicked();
                                        event.accepted = true;
                                    } else {
                                        event.accepted = false;
                                    }
                                }
                            }
                        }
                        ActionButton {
                            id: ticketCorrectionSend
                            objectName: "ticketCorrectionSend"
                            property bool generationActive: !!root.generatingRuns[root.detail.run ? root.detail.run.id : ""] || conversationView.hasLiveMessage
                            text: generationActive ? "Interrupt & correct" : "Send correction"
                            primary: true
                            dark: root.dark
                            enabled: root.selectedSession.length > 0 && !!root.detail.execution && !!root.detail.execution.run_id && ticketCorrectionInput.text.trim().length > 0
                            onClicked: {
                                var correction = ticketCorrectionInput.text.trim();
                                root.request("feedback", {ticket: root.selectedTicket, text: correction, interrupt: generationActive});
                                ticketCorrectionInput.clear();
                            }
                        }
                    }
                }
            }
        }
    }

    Dialog {
        id: newTicketDialog; anchors.centerIn: parent; width: 670; modal: true; title: "Create a ticket"
        onOpened: {
            newTitle.text = root.pendingDraftTitle
            newDescription.text = root.pendingDraftDescription
            newReady.checked = false
            newMergePolicy.currentIndex = 0
            newTeam.currentIndex = Math.max(0, root.validTeams().findIndex(function(t) { return t.ref === root.settings.default_team; }))
            Qt.callLater(function() {
                var team = root.validTeams()[newTeam.currentIndex]
                if (team)
                    newProcess.currentIndex = Math.max(0, team.team.processes.findIndex(function(p) { return p.id === root.settings.default_process; }))
                else
                    newProcess.currentIndex = -1
            })
        }
        contentItem: ColumnLayout {
            spacing: 13
            Text { visible: root.pendingSourceDiscussion.length > 0; text: "Source discussion: " + root.pendingSourceDiscussionTitle; color: root.muted; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            Input { id: newTitle; dark: root.dark; Layout.fillWidth: true; placeholderText: "Ticket title" }
            ScrollView { Layout.fillWidth: true; Layout.preferredHeight: 210; TextArea { id: newDescription; selectByMouse: true; wrapMode: TextEdit.Wrap; color: root.ink; placeholderText: "Objective, scope, constraints and known expectations. Step 2 will formalize verification requirements." } }
            ComboBox {
                id: newTeam
                Layout.fillWidth: true
                model: root.validTeams().map(function(t) { return t.team.name + " / " + t.scope; })
                onActivated: newProcess.currentIndex = 0
            }
            ComboBox { id: newProcess; Layout.fillWidth: true; model: root.validTeams()[newTeam.currentIndex] ? root.validTeams()[newTeam.currentIndex].team.processes.map(function(p) { return p.name; }) : [] }
            CheckBox { id: newReady; text: "Create in Ready (does not start execution)" }
            RowLayout { Text { text: "Merge policy"; color: root.muted } ComboBox { id: newMergePolicy; Layout.fillWidth: true; model: ["Use project setting", "Inspect before merge", "Merge on completion"] } }
            RowLayout {
                Item { Layout.fillWidth: true }
                ActionButton { text: "Cancel"; dark: root.dark; onClicked: newTicketDialog.close() }
                ActionButton { text: "Create ticket"; primary: true; dark: root.dark; enabled: newTitle.text.trim().length > 0 && newTeam.currentIndex >= 0 && newProcess.currentIndex >= 0; onClicked: { var bundle = root.validTeams()[newTeam.currentIndex]; root.request("create", {title: newTitle.text, description: newDescription.text, team_ref: bundle.ref, process_id: bundle.team.processes[newProcess.currentIndex].id, ready: newReady.checked, merge_auto: newMergePolicy.currentIndex === 0 ? null : newMergePolicy.currentIndex === 2, source_discussion_id: root.pendingSourceDiscussion || null}); } }
            }
        }
    }
    Dialog {
        id: discussionRenameDialog; anchors.centerIn: parent; width: 520; modal: true; title: "Rename discussion"
        standardButtons: Dialog.Save | Dialog.Cancel
        contentItem: ColumnLayout { Input { id: discussionRenameText; dark: root.dark; Layout.fillWidth: true; placeholderText: "Discussion title" } }
        onAccepted: if (root.selectedDiscussion.id) root.request("discussion_rename", {discussion: root.selectedDiscussion.id, title: discussionRenameText.text})
    }
    Dialog {
        id: editTicketDialog; anchors.centerIn: parent; width: 650; modal: true; title: "Edit ticket"
        standardButtons: Dialog.Save | Dialog.Cancel
        contentItem: ColumnLayout { Input { id: editTitle; dark: root.dark; Layout.fillWidth: true } ScrollView { Layout.fillWidth: true; Layout.preferredHeight: 250; TextArea { id: editDescription; color: root.ink; wrapMode: TextEdit.Wrap; selectByMouse: true; readOnly: root.detail.ticket && root.detail.ticket.state === "In Progress" } } }
        onAccepted: { var data = {ticket: root.selectedTicket, revision: root.detail.ticket.revision, title: editTitle.text}; if (root.detail.ticket.state !== "In Progress") data.description = editDescription.text; root.request("edit_ticket", data); }
    }
    Dialog {
        id: answerDialog; anchors.centerIn: parent; width: 650; modal: true; title: "Answer the paused team member"
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: ColumnLayout { RenderedText { id: questionText; Layout.fillWidth: true; sourceText: ""; dark: root.dark } ScrollView { Layout.fillWidth: true; Layout.preferredHeight: 180; TextArea { id: answerText; color: root.ink; selectByMouse: true; wrapMode: TextEdit.Wrap } } }
        onAccepted: root.request("answer", {id: root.pendingQuestion, answer: answerText.text})
    }
    Dialog {
        id: advancedDialog; anchors.centerIn: parent; width: Math.min(root.width - 100, 940); height: root.height - 140; modal: true; title: "Advanced settings — validated JSON"
        contentItem: ColumnLayout {
            Text { text: "Edit model profiles, allow/ask/deny tool_rules, MCP servers, setup commands, budgets and cleanup. Credentials should be environment-variable references."; wrapMode: Text.WordWrap; Layout.fillWidth: true; color: root.muted; font.pixelSize: 12 }
            ScrollView { Layout.fillWidth: true; Layout.fillHeight: true; clip: true; TextArea { id: advancedText; color: root.ink; font.family: "monospace"; font.pixelSize: 13; selectByMouse: true; wrapMode: TextEdit.NoWrap } }
            RowLayout { ActionButton { text: "Cancel"; dark: root.dark; onClicked: advancedDialog.close() } Item { Layout.fillWidth: true } ActionButton { text: "Validate & save"; primary: true; dark: root.dark; onClicked: { try { root.request("settings", {settings: JSON.parse(advancedText.text), scope: settingsScope.currentIndex === 0 ? "project" : "global"}); } catch (error) { root.statusMessage = "Invalid JSON: " + error; } } } }
        }
    }
    Dialog { id: modelsDialog; anchors.centerIn: parent; width: 580; height: 360; modal: true; title: "Models listed by the saved endpoint"; standardButtons: Dialog.Close; contentItem: ListView { model: root.models; clip: true; delegate: ItemDelegate { required property string modelData; width: ListView.view.width; text: modelData; onClicked: { modelField.text = modelData; modelsDialog.close(); root.statusMessage = "Model selected in the form. Save settings to apply."; } } } }
    Dialog { id: mergeDialog; anchors.centerIn: parent; width: 580; modal: true; title: "Apply this integration candidate?"; standardButtons: Dialog.Yes | Dialog.No; contentItem: Text { text: "Merge the inspected candidate into " + (root.pendingMerge.target_branch || "") + "?\n\nAgileCode rechecks both commit IDs and requires a clean original checkout. Worktree/checkpoint cleanup follows the configured policy."; color: root.ink; wrapMode: Text.WordWrap }
        onAccepted: root.request("merge", {id: root.pendingMerge.id, candidate: root.pendingMerge.candidate_oid, target: root.pendingMerge.target_oid}) }
    Dialog { id: cancelDialog; anchors.centerIn: parent; width: 560; modal: true; title: "Cancel this ticket run?"; standardButtons: Dialog.Yes | Dialog.No; contentItem: Text { text: "Stop this run and its owned processes? Source, checkpoints and history remain. Interrupted external effects may need reconciliation before work resumes."; color: root.ink; wrapMode: Text.WordWrap }
        onAccepted: root.request("cancel", {ticket: root.selectedTicket}) }
    Dialog {
        id: restoreDialog; anchors.centerIn: parent; width: 700; height: Math.min(650, root.height - 100); modal: true; title: "Inspect checkpoint restoration"
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: ScrollView { TextArea { id: restoreText; color: root.ink; font.family: "monospace"; font.pixelSize: 12; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap } }
        onAccepted: root.request("restore_checkpoint", {ticket: root.selectedTicket, id: root.pendingRestore.checkpoint_id, expected_tree: root.pendingRestore.expected_tree})
    }
    Dialog {
        id: reconcileDialog; anchors.centerIn: parent; width: 640; modal: true; title: "Record inspected outcome"
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: ColumnLayout { Text { text: "Record what you inspected and established. This does not undo external actions or kill an unverified PID."; color: root.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true } ScrollView { Layout.fillWidth: true; Layout.preferredHeight: 170; TextArea { id: reconcileText; color: root.ink; selectByMouse: true; wrapMode: TextEdit.Wrap } } CheckBox { id: notExecuted; text: "I confirmed this tool never executed; allow its original call to be retried"; visible: root.pendingReconcile.kind === "tool" } }
        onAccepted: { root.request("reconcile", {id: root.pendingReconcile.item.id, kind: root.pendingReconcile.kind, explanation: reconcileText.text, not_executed: notExecuted.checked}); root.request("recovery"); }
    }
    Dialog {
        id: pathDialog; property string operation: "backup"; anchors.centerIn: parent; width: 680; modal: true; title: operation === "backup" ? "Create portable project backup" : "Export ticket history"
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: ColumnLayout { Text { text: "Choose a new output path. Existing files are never overwritten. Backups require an idle engine and a clean original checkout."; color: root.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true } Input { id: pathField; dark: root.dark; Layout.fillWidth: true } }
        onAccepted: root.request(operation, {ticket: root.selectedTicket, path: pathField.text})
    }
    Dialog { id: errorDialog; anchors.centerIn: parent; width: Math.min(680, root.width - 80); modal: true; title: "Action needs attention"; standardButtons: Dialog.Ok; contentItem: Text { id: errorText; color: root.ink; wrapMode: Text.WrapAnywhere; font.pixelSize: 14 } }
    Dialog { id: quitDialog; anchors.centerIn: parent; width: 600; modal: true; title: "Close AgileCode?"; standardButtons: Dialog.Yes | Dialog.No; contentItem: Text { text: "Closing this host stops model requests and owned shells. Completed steps and conversations are retained. Shell environment state will not survive. Unknown effects may require inspection on restart."; color: root.ink; wrapMode: Text.WordWrap }
        onAccepted: { root.closingConfirmed = true; root.close(); } }
}
