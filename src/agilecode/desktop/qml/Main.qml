import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtCore
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
    property var availableTools: []
    property var skills: []
    property var settings: ({})
    property var providerPool: ({format_version: 1, default_provider_id: null, providers: []})
    property var detail: ({})
    property var epicDetail: ({})
    property bool epicEditing: false
    property var expandedStepOutputs: ({})
    property var expandedStepDiffs: ({})
    property var pendingDeleteAttempt: ({})
    property var stepDiffCache: ({})
    property var stepDiffRequests: ({})
    property string projectPath: "Opening project…"
    property string selectedTicket: ""
    property string selectedSession: ""
    property var transcript: []
    property string conversationRequestId: ""
    property string conversationOlderRequestId: ""
    property bool conversationHasOlder: false
    property var fileList: []
    property var models: []
    property string statusMessage: "Starting headless engine…"
    property int currentPage: 0
    property bool engineReady: false
    property bool globalExecutionBusy: false
    property string pendingQuestion: ""
    property var pendingRestore: ({})
    property var pendingMerge: ({})
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
    property var pmState: ({status: "paused", goals: [], plan: [], current_activity: "Idle"})
    property var pmSettings: ({mode: "restricted", provider_id: null, max_concurrent_tickets: 4, wait_for_current_tickets: false, answer_ticket_questions: true, uncertainty: "continue", allow_archive: false, allow_delete: false})
    property var pmTranscript: []
    property string pmConversationRequestId: ""
    property string pmConversationOlderRequestId: ""
    property bool pmConversationHasOlder: false
    property var pmProfiles: []
    property var pmProfileSkillIds: []
    property var pmProjectSkillIds: []
    property string pmEditingProfileId: ""
    property bool pmCreatingProfile: false
    property string pmPendingProfileId: ""
    property string pmLoadedSettingsJson: ""
    property string pmLoadedProfilesJson: ""
    property bool pmBusy: false
    // Only board-panel presentation preferences are stored here. PM execution
    // state and conversation history continue to be owned by the backend.
    property bool pmPanelExpanded: false
    property int pmPanelHeight: 320
    Settings {
        category: "BoardProjectManagerPanel"
        property alias expanded: root.pmPanelExpanded
        property alias expandedHeight: root.pmPanelHeight
    }
    property string pmPendingMessageDeletionId: ""
    property bool closingConfirmed: false
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
    function providerEntries() { return providerPool && providerPool.providers ? providerPool.providers : []; }
    function defaultProviderEntry() {
        var entries = providerEntries();
        for (var i = 0; i < entries.length; ++i)
            if (entries[i].id === providerPool.default_provider_id) return entries[i];
        return null;
    }
    function providerChoiceLabels() {
        var def = defaultProviderEntry();
        var labels = ["Default" + (def ? " — " + def.name : " (none)")];
        providerEntries().forEach(function(provider) {
            var model = provider.profile && provider.profile.model ? " · " + provider.profile.model : "";
            labels.push(provider.name + model);
        });
        return labels;
    }
    function providerChoiceId(index) {
        var entries = providerEntries();
        return index > 0 && index - 1 < entries.length ? entries[index - 1].id : null;
    }
    function providerChoiceIndex(providerId) {
        if (!providerId) return 0;
        var entries = providerEntries();
        for (var i = 0; i < entries.length; ++i)
            if (entries[i].id === providerId) return i + 1;
        return 0;
    }
    function providerDisplay(providerId) {
        var entries = providerEntries();
        if (!providerId) {
            var def = defaultProviderEntry();
            return def ? "Default — " + def.name : "Default provider";
        }
        for (var i = 0; i < entries.length; ++i)
            if (entries[i].id === providerId) return entries[i].name;
        return providerId;
    }
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
        pendingDraftDescription = draftDescription || "";
        newTicketDialog.open();
    }
    function filtered(state) {
        var term = searchField.text.toLowerCase();
        return tickets.filter(function(ticket) { return ticket.state === state && (ticket.title.toLowerCase().indexOf(term) >= 0 || ticket.key.toLowerCase().indexOf(term) >= 0); });
    }
    function openEpic(identity) {
        request("epic_detail", {epic_id: identity});
    }
    function loadEpicEditor() {
        if (!epicDetail.epic) return;
        epicTitleField.text = epicDetail.epic.title || "";
        epicDescriptionField.text = epicDetail.epic.description || "";
        epicCriteriaField.text = epicDetail.epic.completion_criteria || "";
        epicStatusField.currentIndex = Math.max(0, ["Active", "Completed", "Archived"].indexOf(epicDetail.epic.status));
    }
    function openTicket(identity) { if (selectedTicket !== identity) { detail = ({}); expandedStepOutputs = ({}); expandedStepDiffs = ({}); stepDiffCache = ({}); stepDiffRequests = ({}); selectedSession = ""; transcript = []; conversationRequestId = ""; conversationOlderRequestId = ""; conversationHasOlder = false; fileList = []; selectedJob = ""; } selectedTicket = identity; request("detail", {ticket: identity}); inspector.open(); }
    function toggleStepDiff(identity) {
        var updated = Object.assign({}, expandedStepDiffs);
        updated[identity] = updated[identity] !== true;
        expandedStepDiffs = updated;
        var cached = stepDiffCache[identity];
        if (!updated[identity] || (cached && !cached.error)) return;
        var nextCache = Object.assign({}, stepDiffCache);
        nextCache[identity] = {loading: true, changed: false, text: "", error: ""};
        stepDiffCache = nextCache;
        var requestId = request("step_diff", {ticket: selectedTicket, attempt: identity});
        var requests = Object.assign({}, stepDiffRequests);
        requests[requestId] = {ticket: selectedTicket, attempt: identity};
        stepDiffRequests = requests;
    }
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
        var merged = Object.keys(byId).map(function(key) { return byId[key]; }).sort(function(a, b) { return a.seq - b.seq; });
        // Saved records are immutable. Don't trigger a ListView reconciliation
        // if this history refresh contains nothing new.
        if (merged.length === existing.length &&
                merged.every(function(row, i) {
                    return row.id === existing[i].id && !!row.partial === !!existing[i].partial;
                }))
            return existing;
        return merged;
    }
    function loadConversation() {
        if (!selectedSession) return;
        conversationRequestId = request("conversation", {session: selectedSession, before: null});
    }
    function loadOlderConversation() {
        if (!selectedSession || transcript.length === 0 || !conversationHasOlder || conversationOlderRequestId.length > 0) return;
        conversationOlderRequestId = request("conversation", {session: selectedSession, before: transcript[0].seq});
    }
    function loadPMConversation() {
        pmConversationRequestId = request("conversation", {session: "project-manager", before: null});
    }
    function loadOlderPMConversation() {
        if (!pmConversationHasOlder || pmTranscript.length === 0 || pmConversationOlderRequestId.length > 0) return;
        pmConversationOlderRequestId = request("conversation", {
            session: "project-manager", before: pmTranscript[0].seq
        });
    }
    function resetPMConversation() {
        pmConversationRefresh.stop();
        pmTranscript = [];
        pmConversationRequestId = "";
        pmConversationOlderRequestId = "";
        pmConversationHasOlder = false;
        pmView.resetHistory();
        loadPMConversation();
    }
    function updatePMTranscript(incoming) {
        // Avoid rebuilding saved delegates when a refresh has no new records.
        var changed = incoming.length !== pmTranscript.length;
        if (!changed) for (var i = 0; i < incoming.length; ++i) {
            if (incoming[i].id !== pmTranscript[i].id
                    || !!incoming[i].partial !== !!pmTranscript[i].partial) {
                changed = true;
                break;
            }
        }
        if (changed) pmTranscript = incoming;
    }
    function saveBasicSettings() {
        var updated = JSON.parse(JSON.stringify(settings));
        updated.shell_command_timeout_seconds = shellTimeoutField.text.trim().length > 0 ? Number(shellTimeoutField.text) : null;
        updated.max_corrections = correctionLimitField.text.trim().length ? Number(correctionLimitField.text) : null;
        updated.git_operation_timeout_seconds = gitTimeoutField.text.trim() ? Number(gitTimeoutField.text) : null;
        updated.model_transport_retry_attempts = Number(transportRetryField.text);
        updated.model_transport_retry_delay_cap_seconds = Number(transportBackoffField.text);
        updated.shell_log_max_bytes = logRetentionField.text.trim() ? Math.round(Number(logRetentionField.text) * 1048576) : null;
        updated.command_preview_bytes = Math.round(Number(commandPreviewField.text) * 1024);
        updated.file_read_page_bytes = Math.round(Number(filePageField.text) * 1024);
        updated.tool_result_inline_bytes = Math.round(Number(toolInlineField.text) * 1024);
        updated.tool_result_preview_chars = Number(toolPreviewField.text);
        updated.search_file_budget = searchFilesField.text.trim() ? Number(searchFilesField.text) : null;
        updated.search_time_budget_seconds = searchTimeField.text.trim() ? Number(searchTimeField.text) : null;
        updated.conversation_page_size = Number(conversationPageField.text);
        updated.python_environment = ["auto", "venv", "disabled"][pythonEnvironmentField.currentIndex];
        updated.python_interpreter = pythonInterpreterField.text.trim() || "python3";
        updated.setup_commands = setupCommandsField.text.split("\n").filter(function(t) { return t.trim().length > 0; });
        updated.merge_on_completion = mergeSwitch.checked; updated.theme = lightSwitch.checked ? "light" : "dark";
        updated.open_reasoning_by_default = reasoningOpenSwitch.checked;
        updated.verification_commands = verifyField.text.split("\n").filter(function(t) { return t.trim().length > 0; });
        var available = validTeams();
        if (defaultTeam.currentIndex >= 0 && defaultTeam.currentIndex < available.length) {
            updated.default_team = available[defaultTeam.currentIndex].ref;
            updated.default_process = available[defaultTeam.currentIndex].team.processes[defaultProcess.currentIndex].id;
        }
        request("settings", {settings: updated, scope: settingsScope.currentIndex === 0 ? "project" : "global"});
    }
    function saveToolSettings() {
        try {
            var updated = JSON.parse(JSON.stringify(settings));
            updated.autonomy = autonomySwitch.checked ? "full" : "restricted";
            updated.command_rules = JSON.parse(JSON.stringify(commandRules.rules));
            updated.tool_rules = JSON.parse(toolRulesText.text || "{}");
            updated.mcp_servers = JSON.parse(mcpServersText.text || "[]");
            if (Array.isArray(updated.tool_rules) || updated.tool_rules === null ||
                    !Array.isArray(updated.mcp_servers))
                throw new Error("Tool rules must be an object and MCP servers must be an array.");
            root.request("settings", {settings: updated,
                scope: toolsScope.currentIndex === 0 ? "project" : "global"});
        } catch (error) {
            root.statusMessage = "Invalid tool configuration: " + error;
        }
    }
    function pmProfileLabels() {
        return ["Default Project Manager"].concat(pmProfiles.map(function(p) { return p.name; }));
    }
    function pmProfileChoiceId(index) {
        return index > 0 && pmProfiles[index - 1] ? pmProfiles[index - 1].id : null;
    }
    function pmProfileIndex(id) {
        if (!id) return 0;
        var index = pmProfiles.findIndex(function(p) { return p.id === id; });
        return index < 0 ? 0 : index + 1;
    }
    function currentPMProfile() {
        return pmProfiles.find(function(p) { return p.id === pmEditingProfileId; }) || null;
    }
    function loadPMProfileEditor() {
        var p = currentPMProfile();
        pmCreatingProfile = false;
        pmProfileName.text = p ? p.name : "";
        pmProfilePrompt.text = p ? (p.instructions || "") : "";
        pmProfileSkillIds = p ? (p.skills || []).slice() : [];
    }
    function selectPMProfile(index) {
        pmEditingProfileId = pmProfileChoiceId(index) || "";
        loadPMProfileEditor();
    }
    function startNewPMProfile(copyExisting) {
        var current = copyExisting ? currentPMProfile() : null;
        pmCreatingProfile = true;
        pmEditingProfileId = "";
        pmProfileName.text = current ? current.name + " Copy" : "";
        pmProfilePrompt.text = current ? (current.instructions || "") : "";
        pmProfileSkillIds = current ? (current.skills || []).slice() : [];
    }
    function togglePMSkill(kind, name, checked) {
        var selected = (kind === "profile" ? pmProfileSkillIds : pmProjectSkillIds).slice();
        var index = selected.indexOf(name);
        if (checked && index < 0) selected.push(name);
        if (!checked && index >= 0) selected.splice(index, 1);
        if (kind === "profile") pmProfileSkillIds = selected;
        else pmProjectSkillIds = selected;
    }
    function pmSkillOptions(globalOnly) {
        return skills.filter(function(record) {
            return !record.error && !record.overridden && (!globalOnly || record.scope === "global");
        });
    }
    function applyPMSnapshot(data) {
        if (!data) return;
        pmState = data.state || pmState;
        var settingsData = data.settings || pmSettings;
        var profilesData = data.profiles || [];
        var refreshEditor = pmLoadedSettingsJson !== JSON.stringify(settingsData)
            || pmLoadedProfilesJson !== JSON.stringify(profilesData)
            || pmPendingProfileId !== "";
        pmSettings = settingsData;
        pmProfiles = profilesData;
        if (data.skills) root.skills = data.skills;
        // PM state refreshes do not retrieve or replace conversation history.
        // Transcript pages are loaded through the same endpoint used by tickets.
        if (refreshEditor) {
            pmLoadedSettingsJson = JSON.stringify(settingsData);
            pmLoadedProfilesJson = JSON.stringify(profilesData);
            Qt.callLater(root.loadPMSettings);
        }
    }
    function loadPMSettings() {
        pmMode.currentIndex = pmSettings.mode === "fully_automated" ? 0 : 1;
        var selectedId = pmPendingProfileId === "__default__" ? null : (pmPendingProfileId || pmSettings.profile_id);
        pmPendingProfileId = "";
        pmProfile.currentIndex = pmProfileIndex(selectedId);
        selectPMProfile(pmProfile.currentIndex);
        pmProjectInstructions.text = pmSettings.project_instructions || "";
        pmProjectSkillIds = (pmSettings.project_skills || []).slice();
        pmProvider.currentIndex = providerChoiceIndex(pmSettings.provider_id);
        pmMaxConcurrent.value = Number(pmSettings.max_concurrent_tickets || 4);
        pmWaitCurrent.checked = !!pmSettings.wait_for_current_tickets;
        pmAnswerQuestions.checked = !!pmSettings.answer_ticket_questions;
        pmUncertainty.currentIndex = pmSettings.uncertainty === "ask" ? 1 : 0;
        pmArchive.checked = !!pmSettings.allow_archive;
        pmDelete.checked = !!pmSettings.allow_delete;
    }
    function savePMSettings() {
        request("pm_settings", {settings: {
            mode: pmMode.currentIndex === 0 ? "fully_automated" : "restricted",
            provider_id: providerChoiceId(pmProvider.currentIndex),
            profile_id: pmProfileChoiceId(pmProfile.currentIndex),
            project_instructions: pmProjectInstructions.text,
            project_skills: pmProjectSkillIds,
            max_concurrent_tickets: pmMaxConcurrent.value,
            wait_for_current_tickets: pmWaitCurrent.checked,
            answer_ticket_questions: pmAnswerQuestions.checked,
            uncertainty: pmUncertainty.currentIndex === 1 ? "ask" : "continue",
            allow_archive: pmArchive.checked,
            allow_delete: pmDelete.checked
        }});
    }
    function loadSettings() {
        shellTimeoutField.text = settings.shell_command_timeout_seconds === null || settings.shell_command_timeout_seconds === undefined ? "" : String(settings.shell_command_timeout_seconds);
        correctionLimitField.text = settings.max_corrections === null || settings.max_corrections === undefined ? "" : String(settings.max_corrections);
        autonomySwitch.checked = settings.autonomy === "full"; mergeSwitch.checked = settings.merge_on_completion;
        commandRules.rules = JSON.parse(JSON.stringify(settings.command_rules || []));
        toolRulesText.text = JSON.stringify(settings.tool_rules || {}, null, 2);
        mcpServersText.text = JSON.stringify(settings.mcp_servers || [], null, 2);
        gitTimeoutField.text = settings.git_operation_timeout_seconds == null ? "" : String(settings.git_operation_timeout_seconds);
        transportRetryField.text = String(settings.model_transport_retry_attempts || 3);
        transportBackoffField.text = String(settings.model_transport_retry_delay_cap_seconds === undefined ? 30 : settings.model_transport_retry_delay_cap_seconds);
        logRetentionField.text = settings.shell_log_max_bytes == null ? "" : String(settings.shell_log_max_bytes / 1048576);
        commandPreviewField.text = String((settings.command_preview_bytes || 65536) / 1024);
        filePageField.text = String((settings.file_read_page_bytes || 262144) / 1024);
        toolInlineField.text = String((settings.tool_result_inline_bytes || 300000) / 1024);
        toolPreviewField.text = String(settings.tool_result_preview_chars === undefined ? 40000 : settings.tool_result_preview_chars);
        searchFilesField.text = settings.search_file_budget == null ? "" : String(settings.search_file_budget);
        searchTimeField.text = settings.search_time_budget_seconds == null ? "" : String(settings.search_time_budget_seconds);
        conversationPageField.text = String(settings.conversation_page_size || 200);
        pythonEnvironmentField.currentIndex = Math.max(0, ["auto", "venv", "disabled"].indexOf(settings.python_environment || "auto"));
        pythonInterpreterField.text = settings.python_interpreter || "python3";
        setupCommandsField.text = (settings.setup_commands || []).join("\n");
        lightSwitch.checked = settings.theme === "light"; reasoningOpenSwitch.checked = !!settings.open_reasoning_by_default; verifyField.text = settings.verification_commands.join("\n");
        defaultTeam.currentIndex = Math.max(0, validTeams().findIndex(function(t) { return t.ref === settings.default_team; }));
        Qt.callLater(function() { var t = root.validTeams()[defaultTeam.currentIndex]; if (t) defaultProcess.currentIndex = Math.max(0, t.team.processes.findIndex(function(p) { return p.id === root.settings.default_process; })); });
    }
    onClosing: function(close) { if (!closingConfirmed) { close.accepted = false; quitDialog.open(); } }
    Timer { id: conversationRefresh; interval: 80; onTriggered: root.loadConversation() }
    Timer { id: pmConversationRefresh; interval: 80; onTriggered: root.loadPMConversation() }
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
                if (root.selectedDiscussion.id && event.session_id === discussionView.sessionId)
                    root.discussionHistoryRequestId = root.request("discussion_history", {discussion: root.selectedDiscussion.id});
            } else if (event.kind === "ProjectManagerGenerationStarted") {
                root.pmBusy = true;
                pmView.interrupted = false;
            } else if (event.kind === "ProjectManagerGenerationFinished") {
                root.pmBusy = false;
                pmConversationRefresh.restart();
            } else if (event.kind === "ProjectManagerDelta") {
                var pmMapped = event.data.kind === "text" ? "ModelTextDelta" : (event.data.kind === "reasoning" ? "ModelReasoningDelta" : (event.data.kind === "tool_delta" ? "ModelToolDelta" : ""));
                if (pmMapped) pmView.appendDelta({kind: pmMapped, session_id: event.session_id, data: {request_id: event.data.request_id, value: event.data.value}});
            } else if (event.kind === "ProjectManagerChanged") {
                root.request("pm_snapshot");
                if (event.data && event.data.history_reset) root.resetPMConversation();
                else pmConversationRefresh.restart();
                refreshTimer.restart();
            } else {
                refreshTimer.restart();
                if (event.session_id === root.selectedSession && ["ConversationChanged", "StepAttemptCompleted"].indexOf(event.kind) >= 0)
                    conversationRefresh.restart();
                if (event.kind === "QuestionRequested" || event.kind === "ApprovalRequested") { root.statusMessage = "A ticket needs your input."; root.openTicket(event.ticket_id); }
                if (event.kind === "IntegrationBlocked" || event.kind === "ExecutionInterrupted") root.statusMessage = event.data.error || event.data.message || "Execution interrupted.";
            }
        }
        function onResponse(identity, action, json) {
            var envelope = JSON.parse(json);
            if (action === "step_diff") {
                var pending = root.stepDiffRequests[identity];
                if (!pending) return;
                var remaining = Object.assign({}, root.stepDiffRequests);
                delete remaining[identity];
                root.stepDiffRequests = remaining;
                if (pending.ticket !== root.selectedTicket) return;
                var cache = Object.assign({}, root.stepDiffCache);
                if (envelope.ok && envelope.data.ticket === pending.ticket && envelope.data.attempt === pending.attempt)
                    cache[pending.attempt] = {loading: false, changed: envelope.data.changed, text: envelope.data.text, error: ""};
                else
                    cache[pending.attempt] = {loading: false, changed: false, text: "", error: envelope.ok ? "Response did not match the selected attempt." : envelope.error};
                root.stepDiffCache = cache;
                return;
            }
            if (!envelope.ok) {
                root.statusMessage = envelope.error; errorText.text = envelope.error; errorDialog.open();
                if (action === "discuss") { root.discussionBusy = false; root.generatingDiscussionId = ""; }
                if (action === "discussion_ticket_draft") { root.ticketDraftBusy = false; root.ticketDraftRequestId = ""; }
                if (action === "pm_message") root.pmBusy = false;
                if (action === "pause_all" || action === "resume_all") root.globalExecutionBusy = false;
                if (action === "conversation") {
                    if (identity === root.conversationOlderRequestId) root.conversationOlderRequestId = "";
                    if (identity === root.pmConversationOlderRequestId) root.pmConversationOlderRequestId = "";
                    if (identity === root.pmConversationRequestId) root.pmConversationRequestId = "";
                }
                return;
            }
            var data = envelope.data;
            if (action === "snapshot") { root.tickets = data.board; if (root.availableTools.length === 0) root.availableTools = data.native_tools || []; root.teams = data.teams; root.settings = data.settings; root.providerPool = data.providers || root.providerPool; root.skills = data.skills; root.discussions = data.discussions || []; root.dark = data.settings.theme !== "light"; root.loadSettings(); root.applyPMSnapshot(data.project_manager); if (!root.pmConversationRequestId && root.pmTranscript.length === 0) root.loadPMConversation(); }
            else if (action === "tool_catalog") {
                root.availableTools = data.tools;
                root.statusMessage = data.errors.length ? data.errors.map(function(error) { return error.server + ": " + error.error; }).join("; ") : "Tool discovery complete.";
            }
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
            else if (action === "epic_detail") {
                root.epicDetail = data;
                root.epicEditing = false;
                root.loadEpicEditor();
                epicDialog.open();
            }
            else if (action === "epic_edit") {
                root.epicEditing = false;
                root.request("epic_detail", {epic_id: data.epic.id});
                root.statusMessage = "Epic updated.";
            }
            else if (action === "delete_attempt") {
                root.expandedStepOutputs = ({});
                root.expandedStepDiffs = ({});
                root.stepDiffCache = ({});
                root.stepDiffRequests = ({});
                root.pendingDeleteAttempt = ({});
                root.selectedSession = "";
                root.transcript = [];
                root.conversationRequestId = "";
                root.conversationOlderRequestId = "";
                root.conversationHasOlder = false;
                conversationView.resetLive();
                inspectTabs.currentIndex = 1;
                root.statusMessage = "Deleted " + data.deleted + " attempt(s). Ticket rewound and paused." +
                    ((data.cleanup_warnings || []).length ? " Some old checkpoint or artifact files could not be cleaned up." : "");
                root.request("detail", {ticket: data.ticket});
                root.refresh();
            }
            else if (action === "delete_ticket") {
                inspector.close();
                root.selectedTicket = "";
                root.detail = ({});
                root.expandedStepOutputs = ({});
                root.expandedStepDiffs = ({});
                root.stepDiffCache = ({});
                root.stepDiffRequests = ({});
                root.selectedSession = "";
                root.transcript = [];
                root.conversationRequestId = "";
                root.conversationOlderRequestId = "";
                root.conversationHasOlder = false;
                root.fileList = [];
                root.selectedJob = "";
                conversationView.resetLive();
                root.statusMessage = data.key + " permanently deleted.";
                root.refresh();
            }
            else if (action === "create") { newTicketDialog.close(); root.pendingSourceDiscussion = ""; root.pendingSourceDiscussionTitle = ""; root.pendingDraftTitle = ""; root.pendingDraftDescription = ""; root.openTicket(data.id); root.refresh(); }
            else if (action === "conversation") {
                if (data.session === "project-manager") {
                    var pmPage = (data.messages || []).filter(function(record) {
                        return record.body && record.body.role !== "system";
                    });
                    if (identity === root.pmConversationOlderRequestId) {
                        root.updatePMTranscript(root.mergeConversationRows(pmPage, root.pmTranscript));
                        root.pmConversationHasOlder = data.has_older;
                        root.pmConversationOlderRequestId = "";
                    } else if (identity === root.pmConversationRequestId) {
                        var pmWasEmpty = root.pmTranscript.length === 0;
                        root.updatePMTranscript(root.mergeConversationRows(root.pmTranscript, pmPage));
                        if (pmWasEmpty) root.pmConversationHasOlder = data.has_older;
                    }
                } else if (data.session === root.selectedSession && identity === root.conversationOlderRequestId) {
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
            else if (action === "models" || action === "provider_models") { root.models = data; modelsDialog.open(); }
            else if (["providers", "save_provider", "default_provider", "delete_provider"].indexOf(action) >= 0) {
                root.providerPool = data;
                root.loadPMSettings();
                root.statusMessage = "Provider Pool updated.";
                if (action === "save_provider") providerDialog.close();
            }
            else if (action === "settings" || action === "reset_settings" || action === "save_team") { root.request("snapshot"); root.statusMessage = "Configuration saved."; advancedDialog.close(); }
            else if (action === "preview_checkpoint") { root.pendingRestore = data; restoreText.text = JSON.stringify(data, null, 2); restoreDialog.open(); }
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
                    // Keep any next-turn deltas received while history was loading.
                    // ConversationView promotes a live row when its saved turn arrives.
                }
            }
            else if (action === "pause_all" || action === "resume_all") {
                root.globalExecutionBusy = false;
                if (action === "pause_all") root.pmBusy = false;
                root.request("pm_snapshot");
                root.refresh();
                if (root.selectedTicket && inspector.opened) root.request("detail", {ticket: root.selectedTicket});
                var verb = action === "pause_all" ? "Paused " : "Resumed ";
                var pmText = data.project_manager_status === "off" ? "Project Manager remains Off."
                    : (data.project_manager_status === "paused" ? "Project Manager paused." : "Project Manager running.");
                root.statusMessage = verb + data.tickets + " ticket(s). " + pmText;
                if (data.errors && data.errors.length) {
                    errorText.text = "Some executions could not be updated:\n" + data.errors.join("\n");
                    errorDialog.open();
                }
            }
            else if (action === "pm_delete_message" || action === "pm_clear_conversation") {
                root.statusMessage = action === "pm_clear_conversation"
                    ? "Project Manager conversation cleared."
                    : "Project Manager message deleted.";
            }
            else if (["pm_snapshot", "pm_resume", "pm_pause", "pm_off", "pm_message"].indexOf(action) >= 0) {
                if (action === "pm_pause" || action === "pm_off") root.pmBusy = false;
                root.applyPMSnapshot(data);
                if (action === "pm_message") pmConversationRefresh.restart();
                root.refresh();
            }
            else if (action === "pm_profile_save") {
                root.pmPendingProfileId = data.id;
                root.request("pm_snapshot");
                root.statusMessage = "Global Project Manager profile saved.";
            }
            else if (action === "pm_profile_delete") {
                root.pmPendingProfileId = "__default__";
                root.request("pm_snapshot");
                root.statusMessage = "Project Manager profile deleted.";
            }
            else if (action === "pm_profiles") {
                root.pmProfiles = data;
                root.loadPMSettings();
            }
            else if (action === "pm_settings") {
                root.pmSettings = data;
                root.loadPMSettings();
                root.request("pm_snapshot");
                root.statusMessage = "Project Manager settings saved.";
            }
            else if (action === "pm_stop") {
                root.pmBusy = false;
                root.request("pm_snapshot");
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
            ActionButton {
                objectName: "pauseAll"
                text: "Pause All"
                danger: true; dark: root.dark
                enabled: root.engineReady && !root.globalExecutionBusy
                onClicked: {
                    root.globalExecutionBusy = true;
                    root.statusMessage = "Pausing tickets and Project Manager…";
                    root.request("pause_all");
                }
            }
            ActionButton {
                objectName: "resumeAll"
                text: "Resume All"
                primary: true; dark: root.dark
                enabled: root.engineReady && !root.globalExecutionBusy
                onClicked: {
                    root.globalExecutionBusy = true;
                    root.statusMessage = "Resuming tickets and Project Manager…";
                    root.request("resume_all");
                }
            }
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
                    model: ["Ticket board", "Project discussions", "Project Manager", "Teams & processes", "Provider Pool", "Settings", "Tools"]
                    delegate: ActionButton {
                        required property string modelData
                        required property int index
                        text: modelData; dark: root.dark; primary: root.currentPage === index
                        Layout.fillWidth: true
                        onClicked: { root.currentPage = index; if (index === 0) root.loadPMConversation(); if (index === 1) root.refreshDiscussions(); if (index === 2) root.request("pm_snapshot"); }
                    }
                }
                Item { Layout.fillHeight: true }
                Rectangle {
                    Layout.fillWidth: true; height: 113; radius: 8; color: root.dark ? "#20342f" : "#e2f2e9"
                    Column {
                        anchors.fill: parent; anchors.margins: 12; spacing: 7
                        Text { text: "DEFAULT PROVIDER"; font.pixelSize: 10; font.letterSpacing: 1; color: root.dark ? "#9bddc3" : "#24694e" }
                        Text { width: parent.width; text: root.defaultProviderEntry() ? (root.defaultProviderEntry().name + (root.defaultProviderEntry().profile.model ? " · " + root.defaultProviderEntry().profile.model : "")) : "Not configured"; wrapMode: Text.WordWrap; maximumLineCount: 2; font.pixelSize: 12; color: root.ink }
                        Text { text: "Independent ticket worktrees"; font.pixelSize: 10; color: root.muted }
                    }
                }
            }
        }
        StackLayout {
            currentIndex: root.currentPage; Layout.fillWidth: true; Layout.fillHeight: true
            Item {
                id: boardPage
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 25; spacing: 14
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
                        Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 160; spacing: 14
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
                                            onEpicSelected: function(epicId) { root.openEpic(epicId); }
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
                    Rectangle {
                        id: boardProjectManagerPanel
                        objectName: "boardProjectManagerPanel"
                        Layout.fillWidth: true
                        Layout.minimumHeight: root.pmPanelExpanded ? 265 : 46
                        Layout.preferredHeight: root.pmPanelExpanded
                            ? Math.min(root.pmPanelHeight, Math.max(265, boardPage.height - 350)) : 46
                        radius: 10
                        color: root.panel
                        border.color: root.borderColor
                        clip: true

                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 0
                            Item {
                                id: projectManagerResizeHandle
                                objectName: "projectManagerResizeHandle"
                                visible: root.pmPanelExpanded
                                Layout.fillWidth: true
                                Layout.preferredHeight: root.pmPanelExpanded ? 12 : 0
                                Rectangle {
                                    anchors.centerIn: parent
                                    width: 54; height: 3; radius: 2
                                    color: root.muted
                                    opacity: 0.55
                                }
                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.SizeVerCursor
                                    property real pressY: 0
                                    property real initialHeight: 0
                                    onPressed: function(mouse) {
                                        pressY = mapToItem(boardPage, mouse.x, mouse.y).y;
                                        initialHeight = boardProjectManagerPanel.height;
                                    }
                                    onPositionChanged: function(mouse) {
                                        if (!pressed) return;
                                        var y = mapToItem(boardPage, mouse.x, mouse.y).y;
                                        var limit = Math.max(265, boardPage.height - 350);
                                        root.pmPanelHeight = Math.round(Math.max(265,
                                            Math.min(limit, initialHeight + pressY - y)));
                                    }
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 46
                                Layout.leftMargin: 12
                                Layout.rightMargin: 12
                                spacing: 10
                                ToolButton {
                                    objectName: "projectManagerPanelToggle"
                                    text: root.pmPanelExpanded ? "▾" : "▸"
                                    font.pixelSize: 16
                                    palette.buttonText: root.ink
                                    Accessible.name: root.pmPanelExpanded ? "Collapse Project Manager conversation" : "Expand Project Manager conversation"
                                    onClicked: root.pmPanelExpanded = !root.pmPanelExpanded
                                }
                                Rectangle {
                                    width: 7; height: 7; radius: 4
                                    color: root.pmState.status === "running" ? "#8ad8bd" : root.muted
                                }
                                Text {
                                    text: "Project Manager"
                                    color: root.ink
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                }
                                Text {
                                    text: root.pmState.status === "running" ? "Running"
                                        : (root.pmState.status === "off" ? "Off" : "Paused")
                                    color: root.muted
                                    font.pixelSize: 12
                                }
                                Text {
                                    text: root.pmBusy ? "Responding…" : (root.pmState.current_activity || "")
                                    color: root.muted
                                    font.pixelSize: 11
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                ActionButton {
                                    text: "Bottom"
                                    dark: root.dark
                                    visible: root.pmPanelExpanded
                                    enabled: root.pmTranscript.length > 0 || pmView.hasLiveMessage
                                    onClicked: pmView.jumpToTail()
                                }
                                ActionButton {
                                    text: "Clear chat"
                                    dark: root.dark
                                    danger: true
                                    visible: root.pmPanelExpanded
                                    enabled: root.pmState.status !== "running" && root.pmTranscript.length > 0
                                    onClicked: pmClearConversationDialog.open()
                                    ToolTip.visible: hovered && root.pmState.status === "running"
                                    ToolTip.text: "Pause the Project Manager before clearing conversation history."
                                }
                            }
                            ColumnLayout {
                                visible: root.pmPanelExpanded
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                Layout.leftMargin: 12
                                Layout.rightMargin: 12
                                Layout.bottomMargin: 10
                                spacing: 8
                                ConversationView {
                                    id: pmView
                                    objectName: "projectManagerConversation"
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    Layout.minimumHeight: 92
                                    sessionId: "project-manager"
                                    messages: root.pmTranscript
                                    dark: root.dark
                                    reasoningExpandedByDefault: !!root.settings.open_reasoning_by_default
                                    assistantName: "Project Manager"
                                    allowMessageDeletion: root.pmState.status !== "running"
                                    onMessageDeletionRequested: function(messageId) {
                                        root.pmPendingMessageDeletionId = messageId;
                                        pmMessageDeleteDialog.open();
                                    }
                                    onOlderHistoryRequested: root.loadOlderPMConversation()
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 8
                                    ScrollView {
                                        id: pmComposer
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: Math.min(94, Math.max(42, pmInput.contentHeight + 18))
                                        contentWidth: availableWidth
                                        clip: true
                                        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                                        TextArea {
                                            id: pmInput
                                            objectName: "projectManagerInput"
                                            width: pmComposer.availableWidth
                                            placeholderText: root.pmState.status === "running" ? "Correct or direct the Project Manager…" : "Add direction for the Project Manager…"
                                            wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
                                            selectByMouse: true
                                            padding: 9
                                            font.pixelSize: 14
                                            color: root.ink
                                            placeholderTextColor: root.muted
                                            background: Rectangle {
                                                color: root.dark ? "#101c27" : "white"
                                                radius: 7
                                                border.color: pmInput.activeFocus ? "#8ad8bd" : root.borderColor
                                            }
                                            Keys.onReturnPressed: function(event) {
                                                if (!(event.modifiers & Qt.ShiftModifier) && pmSend.enabled) {
                                                    pmSend.clicked()
                                                    event.accepted = true
                                                } else event.accepted = false
                                            }
                                        }
                                    }
                                    ActionButton {
                                        id: pmSend
                                        text: "Send"
                                        primary: true
                                        dark: root.dark
                                        enabled: pmInput.text.trim().length > 0
                                        onClicked: {
                                            var message = pmInput.text.trim()
                                            root.pmBusy = root.pmState.status === "running"
                                            root.request("pm_message", {text: message})
                                            pmInput.clear()
                                        }
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

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 24
                    spacing: 12

                    Text { text: "Project Manager"; color: root.ink; font.pixelSize: 27; font.weight: Font.DemiBold }

                    TabBar {
                        id: pmTabs
                        Layout.fillWidth: true
                        TabButton { text: "Runtime" }
                        TabButton { text: "Settings" }
                    }

                    StackLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        currentIndex: pmTabs.currentIndex

                        Item {
                            ColumnLayout {
                                anchors.fill: parent
                                spacing: 10

                                RowLayout {
                                    Layout.fillWidth: true
                                    Text {
                                        text: root.pmState.status === "running" ? "Running" : (root.pmState.status === "off" ? "Off" : "Paused")
                                        color: root.pmState.status === "running" ? "#8ad8bd" : root.muted
                                        font.pixelSize: 12
                                    }
                                    Item { Layout.fillWidth: true }
                                    ActionButton {
                                        text: root.pmState.status === "running" ? "Pause" : (root.pmState.status === "off" ? "Turn On" : "Resume")
                                        primary: root.pmState.status !== "running"
                                        dark: root.dark
                                        onClicked: root.request(root.pmState.status === "running" ? "pm_pause" : "pm_resume")
                                    }
                                    ActionButton {
                                        text: "Stop current response"
                                        danger: true
                                        dark: root.dark
                                        visible: root.pmBusy
                                        onClicked: root.request("pm_stop")
                                    }
                                    ActionButton {
                                        text: "Turn Off"
                                        dark: root.dark
                                        enabled: root.pmState.status !== "off"
                                        onClicked: root.request("pm_off")
                                    }
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 12
                                    Rectangle {
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 122
                                        clip: true
                                        radius: 8
                                        color: root.panel
                                        border.color: root.borderColor
                                        ColumnLayout {
                                            anchors.fill: parent
                                            anchors.margins: 12
                                            spacing: 5
                                            Text { text: "Current goals"; color: root.ink; font.pixelSize: 14; font.weight: Font.DemiBold }
                                            Repeater {
                                                model: root.pmState.goals || []
                                                delegate: Text {
                                                    required property string modelData
                                                    text: "• " + modelData
                                                    color: root.muted
                                                    font.pixelSize: 12
                                                    wrapMode: Text.WordWrap
                                                    Layout.fillWidth: true
                                                }
                                            }
                                            Text { visible: !(root.pmState.goals || []).length; text: "No current goals."; color: root.muted; font.pixelSize: 12 }
                                        }
                                    }
                                    Rectangle {
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 122
                                        clip: true
                                        radius: 8
                                        color: root.panel
                                        border.color: root.borderColor
                                        ColumnLayout {
                                            anchors.fill: parent
                                            anchors.margins: 12
                                            spacing: 5
                                            Text { text: "Current plan"; color: root.ink; font.pixelSize: 14; font.weight: Font.DemiBold }
                                            Repeater {
                                                model: root.pmState.plan || []
                                                delegate: Text {
                                                    required property string modelData
                                                    required property int index
                                                    text: (index + 1) + ". " + modelData
                                                    color: root.muted
                                                    font.pixelSize: 12
                                                    wrapMode: Text.WordWrap
                                                    Layout.fillWidth: true
                                                }
                                            }
                                            Text { visible: !(root.pmState.plan || []).length; text: "No current plan."; color: root.muted; font.pixelSize: 12 }
                                        }
                                    }
                                }

                                Text {
                                    text: "Current activity: " + (root.pmState.current_activity || "Idle")
                                    color: root.muted
                                    font.pixelSize: 12
                                    Layout.fillWidth: true
                                    Layout.maximumHeight: 60
                                    maximumLineCount: 3
                                    elide: Text.ElideRight
                                    wrapMode: Text.WordWrap
                                }

                            }
                        }

                        ScrollView {
                            clip: true
                            ColumnLayout {
                                width: parent.width - 20
                                spacing: 14
                                Text { text: "Project Manager profile"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                                RowLayout {
                                    Layout.fillWidth: true
                                    ComboBox {
                                        id: pmProfile
                                        Layout.fillWidth: true
                                        model: root.pmProfileLabels()
                                        onActivated: root.selectPMProfile(currentIndex)
                                    }
                                    ActionButton { text: "New"; dark: root.dark; onClicked: root.startNewPMProfile(false) }
                                    ActionButton {
                                        text: "Duplicate"; dark: root.dark
                                        enabled: !!root.currentPMProfile() && !root.pmCreatingProfile
                                        onClicked: root.startNewPMProfile(true)
                                    }
                                    ActionButton {
                                        text: "Delete"; danger: true; dark: root.dark
                                        enabled: !!root.currentPMProfile() && !root.pmCreatingProfile
                                        onClicked: pmProfileDeleteDialog.open()
                                    }
                                }
                                Text {
                                    text: root.pmCreatingProfile ? "New reusable profile"
                                          : (root.pmEditingProfileId ? "Edits to this global profile affect every project using it." : "Using AgileCode's built-in Project Manager role.")
                                    color: root.muted; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true
                                }
                                Text { text: "Profile name"; color: root.muted }
                                Input {
                                    id: pmProfileName; dark: root.dark; Layout.fillWidth: true
                                    enabled: root.pmCreatingProfile || root.pmEditingProfileId.length > 0
                                    placeholderText: "e.g. Research Director"
                                }
                                Text { text: "Role instructions"; color: root.muted }
                                ScrollView {
                                    Layout.fillWidth: true; Layout.preferredHeight: 140; contentWidth: availableWidth; clip: true
                                    TextArea {
                                        id: pmProfilePrompt; width: parent.width; selectByMouse: true
                                        readOnly: !root.pmCreatingProfile && !root.pmEditingProfileId
                                        wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
                                        placeholderText: "Define this Project Manager's role and methodology."
                                        color: root.ink
                                    }
                                }
                                Text { text: "Skills for this profile (global library)"; color: root.muted }
                                Repeater {
                                    model: root.pmSkillOptions(true)
                                    delegate: CheckBox {
                                        required property var modelData
                                        text: modelData.id
                                        ToolTip.visible: hovered
                                        ToolTip.text: modelData.description
                                        enabled: root.pmCreatingProfile || root.pmEditingProfileId.length > 0
                                        checked: root.pmProfileSkillIds.indexOf(modelData.id) >= 0
                                        onClicked: root.togglePMSkill("profile", modelData.id, checked)
                                        Layout.fillWidth: true
                                    }
                                }
                                Text {
                                    visible: root.pmSkillOptions(true).length === 0
                                    text: "No global skills found. Add them under the global skills directory."
                                    color: root.muted; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Item { Layout.fillWidth: true }
                                    ActionButton {
                                        text: root.pmCreatingProfile ? "Create profile" : "Save profile"
                                        primary: true; dark: root.dark
                                        enabled: (root.pmCreatingProfile || root.pmEditingProfileId.length > 0) && pmProfileName.text.trim().length > 0
                                        onClicked: root.request("pm_profile_save", {profile: {
                                            id: root.pmCreatingProfile ? null : root.pmEditingProfileId,
                                            name: pmProfileName.text.trim(),
                                            instructions: pmProfilePrompt.text,
                                            skills: root.pmProfileSkillIds
                                        }})
                                    }
                                }

                                Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }
                                Text { text: "This project's additions"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                                Text {
                                    text: "These instructions and skills apply only to this project, in addition to its selected global profile."
                                    color: root.muted; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true
                                }
                                ScrollView {
                                    Layout.fillWidth: true; Layout.preferredHeight: 135; contentWidth: availableWidth; clip: true
                                    TextArea {
                                        id: pmProjectInstructions; width: parent.width; selectByMouse: true
                                        wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
                                        placeholderText: "Instructions specific to this project…"
                                        color: root.ink
                                    }
                                }
                                Text { text: "Additional project skills"; color: root.muted }
                                Repeater {
                                    model: root.pmSkillOptions(false)
                                    delegate: CheckBox {
                                        required property var modelData
                                        text: modelData.id + " (" + modelData.scope + ")"
                                        ToolTip.visible: hovered
                                        ToolTip.text: modelData.description
                                        checked: root.pmProjectSkillIds.indexOf(modelData.id) >= 0
                                        onClicked: root.togglePMSkill("project", modelData.id, checked)
                                        Layout.fillWidth: true
                                    }
                                }
                                Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }
                                Text { text: "Operating mode"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                                ComboBox {
                                    id: pmMode
                                    Layout.fillWidth: true
                                    model: ["Fully Automated", "Restricted"]
                                }
                                Text {
                                    text: pmMode.currentIndex === 0
                                          ? "The Project Manager independently establishes goals, plans, and manages the project until complete."
                                          : "The Project Manager performs complete user-requested management operations, then waits for another instruction."
                                    color: root.muted
                                    font.pixelSize: 12
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text { text: "Project Manager provider"; color: root.muted; Layout.preferredWidth: 180 }
                                    ComboBox { id: pmProvider; Layout.fillWidth: true; model: root.providerChoiceLabels() }
                                }

                                Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }
                                Text { text: "Scheduling"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text { text: "Maximum concurrent tickets"; color: root.muted; Layout.fillWidth: true }
                                    SpinBox {
                                        id: pmMaxConcurrent; from: 1; to: 2147483647; value: 4
                                        ToolTip.visible: hovered
                                        ToolTip.text: "Maximum tickets the Project Manager may run at once. Set this with your providers' actual concurrent-request capacity in mind, including any capacity you want to leave for Project Manager requests."
                                    }
                                }
                                Switch { id: pmWaitCurrent; text: "Wait until all current tickets finish before starting more" }

                                Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }
                                Text { text: "Decision delegation"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                                Switch { id: pmAnswerQuestions; text: "Allow Project Manager to answer ticket questions" }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text { text: "When uncertain"; color: root.muted; Layout.preferredWidth: 150 }
                                    ComboBox {
                                        id: pmUncertainty
                                        Layout.fillWidth: true
                                        model: ["Make the best decision and continue", "Ask me"]
                                    }
                                }

                                Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }
                                Text { text: "Additional authority"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                                Switch { id: pmArchive; text: "Allow archive tickets" }
                                Switch { id: pmDelete; text: "Allow delete tickets" }

                                RowLayout {
                                    Layout.fillWidth: true
                                    Item { Layout.fillWidth: true }
                                    ActionButton { text: "Save Project Manager settings"; primary: true; dark: root.dark; onClicked: root.savePMSettings() }
                                }
                                Item { Layout.preferredHeight: 30 }
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
                availableTools: root.availableTools
                onDiscoverToolsRequested: root.request("tool_catalog")
                dark: root.dark
                ink: root.ink
                muted: root.muted
                panel: root.panel
                borderColor: root.borderColor
                onSaveRequested: function(team, scope) {
                    root.request("save_team", {team: team, scope: scope});
                }
            }
            Item {
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 25
                    spacing: 14
                    RowLayout {
                        Layout.fillWidth: true
                        ColumnLayout {
                            Layout.fillWidth: true
                            Text { text: "Provider Pool"; color: root.ink; font.pixelSize: 27; font.weight: Font.DemiBold }
                            Text { text: "Global, reusable provider/model configurations. Tickets and the Project Manager use the default unless another provider is explicitly assigned."; color: root.muted; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                        }
                        ActionButton {
                            text: "+ Add Provider"
                            primary: true
                            dark: root.dark
                            onClicked: providerDialog.loadProvider(null)
                        }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }
                    Rectangle {
                        Layout.preferredWidth: Math.min(620, parent.width)
                        Layout.fillHeight: true
                        radius: 9
                        color: root.dark ? "#13202b" : "#f7f9fb"
                        border.color: root.borderColor
                        ListView {
                            anchors.fill: parent
                            anchors.margins: 12
                            clip: true
                            spacing: 10
                            model: root.providerEntries()
                            delegate: Rectangle {
                                required property var modelData
                                width: ListView.view.width
                                height: 126
                                radius: 8
                                color: root.dark ? "#1d2e3c" : "#ffffff"
                                border.color: modelData.id === root.providerPool.default_provider_id ? "#8ad8bd" : root.borderColor
                                ColumnLayout {
                                    anchors.fill: parent
                                    anchors.margins: 12
                                    spacing: 5
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Text { text: modelData.id === root.providerPool.default_provider_id ? "★ " + modelData.name : modelData.name; color: root.ink; font.pixelSize: 15; font.weight: Font.DemiBold; Layout.fillWidth: true; elide: Text.ElideRight }
                                        Text { text: modelData.id; color: root.muted; font.pixelSize: 10 }
                                    }
                                    Text { text: modelData.profile.model || "Use the single model listed by the endpoint"; color: root.ink; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight }
                                    Text { text: modelData.profile.base_url; color: root.muted; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideMiddle }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Item { Layout.fillWidth: true }
                                        ActionButton { text: "Set default"; dark: root.dark; enabled: modelData.id !== root.providerPool.default_provider_id; onClicked: root.request("default_provider", {provider_id: modelData.id}) }
                                        ActionButton { text: "Edit"; dark: root.dark; onClicked: providerDialog.loadProvider(modelData) }
                                        ActionButton { text: "Delete"; danger: true; dark: root.dark; onClicked: root.request("delete_provider", {provider_id: modelData.id}) }
                                    }
                                }
                            }
                            Text {
                                anchors.centerIn: parent
                                visible: root.providerEntries().length === 0
                                text: "No providers configured."
                                color: root.muted
                            }
                        }
                    }
                    Item { Layout.fillHeight: true }
                }
            }
            ScrollView {
                clip: true
                ColumnLayout {
                    width: parent.width - 50; x: 25; y: 24; spacing: 14
                    Text { text: "Settings"; color: root.ink; font.pixelSize: 27; font.weight: Font.DemiBold }
                    Text { text: "Application behavior, operational limits, and project defaults. Provider-owned inference settings remain in the provider."; color: root.muted; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    RowLayout { Text { text: "Save changes to"; color: root.muted } ComboBox { id: settingsScope; model: ["This project", "Global default"] } Item { Layout.fillWidth: true } ActionButton { text: "Advanced JSON"; dark: root.dark; onClicked: { advancedText.text = JSON.stringify(root.settings, null, 2); advancedDialog.open(); } } }
                    Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }

                    Text { text: "Behavior"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                    Switch { id: mergeSwitch; text: "Merge on completion; otherwise hold the candidate for inspection" }
                    Switch { id: reasoningOpenSwitch; text: "Open reasoning blocks by default" }
                    Switch { id: lightSwitch; text: "Light appearance" }
                    GridLayout {
                        columns: 2; Layout.fillWidth: true; rowSpacing: 11; columnSpacing: 18
                        Text { text: "Correction attempt limit"; color: root.muted; Layout.preferredWidth: 150 }
                        Input { id: correctionLimitField; dark: root.dark; Layout.fillWidth: true; inputMethodHints: Qt.ImhDigitsOnly; placeholderText: "Blank = unlimited" }
                        Text { text: "Command timeout (seconds)"; color: root.muted }
                        Input { id: shellTimeoutField; dark: root.dark; Layout.fillWidth: true; inputMethodHints: Qt.ImhFormattedNumbersOnly; placeholderText: "Blank = no timeout" }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }
                    Text { text: "Operational limits"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                    Text { text: "These limits belong to AgileCode, not your model provider. Blank optional limits mean no limit or no timeout."; color: root.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    GridLayout {
                        columns: 2; Layout.fillWidth: true; rowSpacing: 10; columnSpacing: 18
                        Text { text: "Model connection retries"; color: root.muted }
                        Input { id: transportRetryField; dark: root.dark; Layout.fillWidth: true }
                        Text { text: "Maximum retry delay (seconds)"; color: root.muted }
                        Input { id: transportBackoffField; dark: root.dark; Layout.fillWidth: true }
                        Text { text: "Git timeout (seconds)"; color: root.muted }
                        Input { id: gitTimeoutField; objectName: "gitTimeoutField"; dark: root.dark; Layout.fillWidth: true; placeholderText: "Blank = no timeout" }
                        Text { text: "Stored shell log limit (MiB)"; color: root.muted }
                        Input { id: logRetentionField; dark: root.dark; Layout.fillWidth: true; placeholderText: "Blank = keep complete logs" }
                        Text { text: "Command preview (KiB)"; color: root.muted }
                        Input { id: commandPreviewField; dark: root.dark; Layout.fillWidth: true }
                        Text { text: "File read page (KiB)"; color: root.muted }
                        Input { id: filePageField; dark: root.dark; Layout.fillWidth: true }
                        Text { text: "Inline tool result (KiB)"; color: root.muted }
                        Input { id: toolInlineField; dark: root.dark; Layout.fillWidth: true }
                        Text { text: "Tool result preview (characters)"; color: root.muted }
                        Input { id: toolPreviewField; dark: root.dark; Layout.fillWidth: true }
                        Text { text: "Files examined per search"; color: root.muted }
                        Input { id: searchFilesField; dark: root.dark; Layout.fillWidth: true; placeholderText: "Blank = no file budget" }
                        Text { text: "Search time budget (seconds)"; color: root.muted }
                        Input { id: searchTimeField; dark: root.dark; Layout.fillWidth: true; placeholderText: "Blank = no time budget" }
                        Text { text: "Conversation messages per page"; color: root.muted }
                        Input { id: conversationPageField; dark: root.dark; Layout.fillWidth: true }
                    }
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
                    Label { text: "Python environment"; color: root.ink }
                    ComboBox { id: pythonEnvironmentField; objectName: "pythonEnvironmentField"; model: ["Automatic for Python projects", "Always create a worktree .venv", "Disabled"]; Layout.fillWidth: true }
                    Label { text: "Python interpreter"; color: root.ink }
                    TextField { id: pythonInterpreterField; objectName: "pythonInterpreterField"; placeholderText: "python3 or an absolute interpreter path"; Layout.fillWidth: true }
                    Label { text: "Worktree setup commands"; color: root.ink }
                    TextArea { id: setupCommandsField; objectName: "setupCommandsField"; placeholderText: "Optional: one command per line. Runs inside the worktree .venv.\nExample: python -m pip install -e '.[test]'"; Layout.fillWidth: true; wrapMode: TextEdit.Wrap; selectByMouse: true }
                    Text { text: "Required verification commands — one per line"; color: root.muted; font.pixelSize: 12 }
                    ScrollView { Layout.fillWidth: true; Layout.preferredHeight: 100; TextArea { id: verifyField; color: root.ink; font.family: "monospace"; selectByMouse: true; placeholderText: "python -m pytest -q"; background: Rectangle { color: root.panel; radius: 7; border.color: root.borderColor } } }
                    RowLayout { ActionButton { text: "Save settings"; primary: true; dark: root.dark; onClicked: root.saveBasicSettings() } ActionButton { text: "Use global defaults"; dark: root.dark; onClicked: root.request("reset_settings") } ActionButton { text: "Create backup…"; dark: root.dark; onClicked: { pathDialog.operation = "backup"; pathField.text = root.projectPath + "/../AgileCode-backup.zip"; pathDialog.open(); } } }
                    Item { Layout.preferredHeight: 40 }
                }
            }
            ScrollView {
                clip: true
                ColumnLayout {
                    width: parent.width - 50; x: 25; y: 24; spacing: 14
                    Text { text: "Tools"; color: root.ink; font.pixelSize: 27; font.weight: Font.DemiBold }
                    Text { text: "Tool permissions, command rules, MCP servers, and discovered skills."; color: root.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    RowLayout {
                        Text { text: "Save changes to"; color: root.muted }
                        ComboBox { id: toolsScope; model: ["This project", "Global default"] }
                        Item { Layout.fillWidth: true }
                        ActionButton { text: "Discover tools"; dark: root.dark; onClicked: root.request("tool_catalog") }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }
                    Switch { id: autonomySwitch; text: "Full tool autonomy (explicit deny/ask rules still apply)" }
                    Text { text: "Command allow / ask / deny rules"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                    CommandRules { id: commandRules; objectName: "commandRules"; Layout.fillWidth: true; dark: root.dark; ink: root.ink; muted: root.muted }
                    Text { text: "Tool rules (JSON)"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                    Text { text: "Map tool names to allow, ask, or deny. Team tool access remains under Teams & processes."; color: root.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    ScrollView {
                        Layout.fillWidth: true; Layout.preferredHeight: 115; contentWidth: availableWidth
                        TextArea { id: toolRulesText; objectName: "toolRulesText"; width: parent.width; color: root.ink; font.family: "monospace"; wrapMode: TextEdit.NoWrap; selectByMouse: true }
                    }
                    Text { text: "MCP server connections (JSON)"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                    Text { text: "Each MCP server can specify timeout_seconds and max_message_bytes. Null means no application-imposed limit."; color: root.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    ScrollView {
                        Layout.fillWidth: true; Layout.preferredHeight: 135; contentWidth: availableWidth
                        TextArea { id: mcpServersText; objectName: "mcpServersText"; width: parent.width; color: root.ink; font.family: "monospace"; wrapMode: TextEdit.NoWrap; selectByMouse: true }
                    }
                    RowLayout {
                        Item { Layout.fillWidth: true }
                        ActionButton { text: "Save tool settings"; primary: true; dark: root.dark; onClicked: root.saveToolSettings() }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }
                    Text { text: "Discovered skills"; color: root.ink; font.pixelSize: 18; font.weight: Font.DemiBold }
                    Text { text: "Skills can be enabled for members under Teams & processes."; color: root.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    Repeater {
                        model: root.skills
                        delegate: Text { required property var modelData; Layout.fillWidth: true; text: modelData.error ? "ERROR · " + modelData.error : (modelData.id + " · " + (modelData.source || modelData.scope || "") + "\n" + markdownPresentation.plainText(modelData.description || "")); wrapMode: Text.WordWrap; color: modelData.error ? "#d6a385" : root.muted; font.pixelSize: 12 }
                    }
                    Item { Layout.preferredHeight: 40 }
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
                ActionButton { text: "Ready"; dark: root.dark; visible: !!root.detail.ticket && root.detail.ticket.state === "Backlog"; onClicked: root.request("move", {ticket: root.selectedTicket, state: "Ready"}) }
                ActionButton { text: "Start"; primary: true; dark: root.dark; visible: !!root.detail.ticket && (root.detail.ticket.state === "Ready" || (root.detail.ticket.state === "In Progress" && (!root.detail.run || ["failed", "cancelled"].indexOf(root.detail.run.state) >= 0))) && !(root.detail.execution && root.detail.execution.can_resume); onClicked: root.request("start", {ticket: root.selectedTicket}) }
                ActionButton {
                    objectName: "ticketResumeExecution"
                    text: "Resume"
                    primary: true
                    dark: root.dark
                    visible: !!root.detail.ticket && root.detail.ticket.state === "In Progress" && !!root.detail.execution && !!root.detail.execution.can_resume
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
                                objectName: "ticketDescriptionOpen"
                                text: "View full description"
                                dark: root.dark
                                visible: !!root.detail.ticket && root.detail.ticket.description.length > 0
                                onClicked: ticketDescriptionDialog.open()
                            }
                        }
                        Rectangle {
                            objectName: "ticketDescriptionPreviewCard"
                            Layout.fillWidth: true
                            Layout.preferredHeight: 118
                            color: root.dark ? "#101923" : "#f7f9fb"
                            radius: 7
                            border.color: root.borderColor
                            clip: true
                            Text {
                                id: ticketDescriptionPreview
                                objectName: "ticketDescriptionPreview"
                                anchors.fill: parent
                                anchors.margins: 10
                                text: root.detail.ticket && root.detail.ticket.description.length > 0 ? root.detail.ticket.description : "No description."
                                textFormat: Text.MarkdownText
                                color: root.detail.ticket && root.detail.ticket.description.length > 0 ? root.ink : root.muted
                                wrapMode: Text.WordWrap
                                font.pixelSize: 14
                                maximumLineCount: 6
                                elide: Text.ElideRight
                                onLinkActivated: function(link) { Qt.openUrlExternally(link); }
                            }
                        }
                        RowLayout {
                            visible: !!root.detail.source_discussion
                            Layout.fillWidth: true
                            Text { text: root.detail.source_discussion ? "SOURCE DISCUSSION · " + root.detail.source_discussion.title : ""; color: root.muted; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                            ActionButton { text: "Open discussion"; dark: root.dark; onClicked: { var source = root.detail.source_discussion; inspector.close(); root.currentPage = 1; if (source.archived) showArchivedDiscussions.checked = true; root.selectDiscussion(source); root.refreshDiscussions(); } }
                        }
                        Text { text: root.detail.ticket ? "Provider: " + root.providerDisplay(root.detail.ticket.provider_id) : ""; color: root.muted; font.pixelSize: 12 }
                        Text { text: root.detail.run ? "Execution: " + root.detail.run.state + "\n" + (root.detail.run.error || "") + "\n\nBranch: " + root.detail.run.branch + "\nBase: " + root.detail.run.base_oid + "\nWorktree: " + root.detail.run.workspace : "No run yet."; color: root.muted; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true; font.pixelSize: 12 }
                        RowLayout {
                            ActionButton { text: "Edit ticket…"; dark: root.dark; onClicked: { editTitle.text = root.detail.ticket.title; editDescription.text = root.detail.ticket.description; editEpicId.text = root.detail.ticket.epic_id || ""; editProvider.currentIndex = root.providerChoiceIndex(root.detail.ticket.provider_id); editTicketDialog.open(); } }
                            ActionButton { text: "Archive"; dark: root.dark; enabled: !!root.detail.ticket && root.detail.ticket.state !== "In Progress"; onClicked: root.request("archive", {ticket: root.selectedTicket}) }
                            ActionButton { objectName: "ticketDelete"; text: "Delete"; danger: true; dark: root.dark; visible: !!root.detail.ticket && root.detail.ticket.state !== "Done"; onClicked: deleteTicketDialog.open() }
                            ActionButton { text: "Reopen"; dark: root.dark; visible: !!root.detail.ticket && root.detail.ticket.state === "Done"; onClicked: root.request("move", {ticket: root.selectedTicket, state: "Backlog"}) }
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
                            id: attemptCard
                            required property var modelData
                            readonly property bool hasStepOutput: !!modelData.result && typeof modelData.result.output === "string"
                            readonly property bool outputExpanded: root.expandedStepOutputs[modelData.id] === true
                            readonly property bool hasStepDiff: !!modelData.result
                            readonly property bool diffExpanded: root.expandedStepDiffs[modelData.id] === true
                            readonly property var diffData: root.stepDiffCache[modelData.id] || ({loading: true, changed: false, text: "", error: ""})
                            width: ListView.view.width
                            height: attemptColumn.implicitHeight + 24
                            radius: 8
                            color: root.dark ? "#1d2e3c" : "#edf3f7"
                            border.color: root.borderColor

                            ColumnLayout {
                                id: attemptColumn
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: 12
                                spacing: 5

                                Item {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 71
                                    ColumnLayout {
                                        anchors.fill: parent
                                        spacing: 5
                                        RowLayout {
                                            Layout.fillWidth: true
                                            Text { text: modelData.step_id.replace(/_/g," ") + " · attempt " + modelData.number; color: root.ink; font.pixelSize: 13; font.weight: Font.DemiBold }
                                            Item { Layout.fillWidth: true }
                                            Text { text: modelData.result ? modelData.result.status : modelData.status; color: modelData.result && modelData.result.status !== "PASS" ? "#ddb08a" : "#8ad8bd"; font.pixelSize: 11 }
                                        }
                                        Text { text: modelData.member.name + " / " + modelData.model.model + " · " + modelData.phase; color: root.muted; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                                        Text { text: modelData.result ? markdownPresentation.plainText(modelData.result.summary) : "Open session to inspect progress and prompts."; color: root.muted; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight }
                                    }
                                    MouseArea {
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: root.selectSession(modelData.session_id)
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 14
                                    ToolButton {
                                        objectName: "stepOutputToggle"
                                        visible: attemptCard.hasStepOutput
                                        text: (attemptCard.outputExpanded ? "▾" : "▸") + " Step Output"
                                        font.pixelSize: 12
                                        palette.buttonText: root.muted
                                        Accessible.name: "Step Output"
                                        Accessible.description: attemptCard.outputExpanded ? "Collapse step output" : "Expand step output"
                                        onClicked: {
                                            var updated = Object.assign({}, root.expandedStepOutputs);
                                            updated[attemptCard.modelData.id] = !attemptCard.outputExpanded;
                                            root.expandedStepOutputs = updated;
                                        }
                                    }
                                    ToolButton {
                                        objectName: "stepDiffToggle"
                                        visible: attemptCard.hasStepDiff
                                        text: (attemptCard.diffExpanded ? "▾" : "▸") + " Step Diff"
                                        font.pixelSize: 12
                                        palette.buttonText: root.muted
                                        Accessible.name: "Step Diff"
                                        Accessible.description: attemptCard.diffExpanded ? "Collapse step diff" : "Expand step diff"
                                        onClicked: root.toggleStepDiff(attemptCard.modelData.id)
                                    }
                                    Item { Layout.fillWidth: true }
                                    ActionButton {
                                        objectName: "deleteStepAttempt"
                                        text: "Delete attempt"
                                        dark: root.dark
                                        danger: true
                                        visible: attemptCard.modelData.phase === "development"
                                                 && !!root.detail.run && !root.detail.run.final_oid
                                                 && !!root.detail.ticket && root.detail.ticket.state !== "Done"
                                        onClicked: {
                                            root.pendingDeleteAttempt = {
                                                id: attemptCard.modelData.id,
                                                step: attemptCard.modelData.step_id.replace(/_/g, " "),
                                                number: attemptCard.modelData.number
                                            };
                                            deleteAttemptDialog.open();
                                        }
                                    }
                                }
                                Loader {
                                    id: stepOutputLoader
                                    active: attemptCard.hasStepOutput && attemptCard.outputExpanded
                                    visible: active
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: item ? item.implicitHeight : 0
                                    sourceComponent: Component {
                                        Rectangle {
                                            implicitHeight: Math.max(70, Math.min(400, outputText.contentHeight + 20))
                                            radius: 6
                                            color: root.dark ? "#101923" : "#ffffff"
                                            border.color: root.borderColor
                                            ScrollView {
                                                id: outputScroll
                                                anchors.fill: parent
                                                anchors.margins: 10
                                                clip: true
                                                contentWidth: availableWidth
                                                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                                                TextArea {
                                                    id: outputText
                                                    width: outputScroll.availableWidth
                                                    text: attemptCard.modelData.result ? attemptCard.modelData.result.output : ""
                                                    textFormat: TextEdit.PlainText
                                                    readOnly: true
                                                    selectByMouse: true
                                                    wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
                                                    color: root.ink
                                                    font.pixelSize: 12
                                                    padding: 0
                                                    background: null
                                                }
                                            }
                                        }
                                    }
                                }
                                Loader {
                                    id: stepDiffLoader
                                    active: attemptCard.hasStepDiff && attemptCard.diffExpanded
                                    visible: active
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: item ? item.implicitHeight : 0
                                    sourceComponent: Component {
                                        StepDiffView {
                                            sourceText: attemptCard.diffData.text
                                            changed: attemptCard.diffData.changed
                                            loading: attemptCard.diffData.loading
                                            error: attemptCard.diffData.error
                                            dark: root.dark
                                        }
                                    }
                                }
                            }
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
        id: epicDialog
        objectName: "epicDetailsDialog"
        anchors.centerIn: parent
        width: Math.min(root.width - 120, 650)
        height: Math.min(root.height - 120, 710)
        modal: true
        title: root.epicDetail.epic ? "Epic · " + root.epicDetail.epic.id : "Epic"
        contentItem: ColumnLayout {
            spacing: 9
            RowLayout {
                Layout.fillWidth: true
                Text {
                    text: root.epicDetail.epic ? root.epicDetail.epic.id : ""
                    color: root.muted; font.pixelSize: 12; font.weight: Font.DemiBold
                }
                Item { Layout.fillWidth: true }
                Text {
                    text: (root.epicDetail.completed_count || 0) + " / " +
                        (root.epicDetail.ticket_count || 0) + " tickets complete"
                    color: root.muted; font.pixelSize: 12
                }
            }
            Text { text: "TITLE"; color: root.muted; font.pixelSize: 10; font.letterSpacing: 1 }
            Input {
                id: epicTitleField
                dark: root.dark
                Layout.fillWidth: true
                readOnly: !root.epicEditing
                selectByMouse: true
            }
            Text { text: "PURPOSE AND SCOPE"; color: root.muted; font.pixelSize: 10; font.letterSpacing: 1 }
            ScrollView {
                id: epicDescriptionScroll
                Layout.fillWidth: true
                Layout.preferredHeight: 105
                clip: true
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                TextArea {
                    id: epicDescriptionField
                    width: epicDescriptionScroll.availableWidth
                    color: root.ink
                    readOnly: !root.epicEditing
                    selectByMouse: true
                    wrapMode: TextEdit.Wrap
                    textFormat: TextEdit.PlainText
                }
            }
            Text { text: "COMPLETION CRITERIA"; color: root.muted; font.pixelSize: 10; font.letterSpacing: 1 }
            ScrollView {
                id: epicCriteriaScroll
                Layout.fillWidth: true
                Layout.preferredHeight: 105
                clip: true
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                TextArea {
                    id: epicCriteriaField
                    width: epicCriteriaScroll.availableWidth
                    color: root.ink
                    readOnly: !root.epicEditing
                    selectByMouse: true
                    wrapMode: TextEdit.Wrap
                    textFormat: TextEdit.PlainText
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Text { text: "STATUS"; color: root.muted; font.pixelSize: 10; font.letterSpacing: 1 }
                ComboBox {
                    id: epicStatusField
                    Layout.fillWidth: true
                    model: ["Active", "Completed", "Archived"]
                    enabled: root.epicEditing
                }
            }
            Text { text: "ASSOCIATED TICKETS"; color: root.muted; font.pixelSize: 10; font.letterSpacing: 1 }
            ListView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: root.epicDetail.tickets || []
                ScrollBar.vertical: ScrollBar {}
                delegate: ItemDelegate {
                    required property var modelData
                    width: ListView.view.width
                    text: modelData.key + " · " + modelData.title + " — " + modelData.state +
                        (modelData.archived ? " (archived)" : "")
                    onClicked: {
                        epicDialog.close();
                        root.openTicket(modelData.id);
                    }
                }
                Text {
                    anchors.centerIn: parent
                    visible: parent.count === 0
                    text: "No tickets in this epic."
                    color: root.muted
                    font.pixelSize: 12
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ActionButton {
                    text: "Edit"; dark: root.dark
                    visible: !root.epicEditing
                    onClicked: root.epicEditing = true
                }
                ActionButton {
                    text: "Cancel"; dark: root.dark
                    visible: root.epicEditing
                    onClicked: { root.epicEditing = false; root.loadEpicEditor(); }
                }
                ActionButton {
                    text: "Save"; primary: true; dark: root.dark
                    visible: root.epicEditing
                    enabled: epicTitleField.text.trim().length > 0
                    onClicked: root.request("epic_edit", {
                        epic_id: root.epicDetail.epic.id,
                        revision: root.epicDetail.epic.revision,
                        title: epicTitleField.text,
                        description: epicDescriptionField.text,
                        completion_criteria: epicCriteriaField.text,
                        status: epicStatusField.currentText
                    })
                }
                ActionButton {
                    text: "Close"; dark: root.dark
                    onClicked: epicDialog.close()
                }
            }
        }
    }
    Dialog {
        id: newTicketDialog; anchors.centerIn: parent; width: 670; modal: true; title: "Create a ticket"
        onOpened: {
            newTitle.text = root.pendingDraftTitle
            newDescription.text = root.pendingDraftDescription
            newEpicId.text = ""
            newReady.checked = false
            newProvider.currentIndex = 0
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
            Input { id: newEpicId; dark: root.dark; Layout.fillWidth: true; placeholderText: "Epic ID (optional, e.g. AgentGUIInt)" }
            ScrollView { Layout.fillWidth: true; Layout.preferredHeight: 210; TextArea { id: newDescription; selectByMouse: true; textFormat: TextEdit.PlainText; wrapMode: TextEdit.Wrap; color: root.ink; placeholderText: "Markdown supported. Describe the objective, scope, constraints and known expectations." } }
            ComboBox {
                id: newTeam
                Layout.fillWidth: true
                model: root.validTeams().map(function(t) { return t.team.name + " / " + t.scope; })
                onActivated: newProcess.currentIndex = 0
            }
            ComboBox { id: newProcess; Layout.fillWidth: true; model: root.validTeams()[newTeam.currentIndex] ? root.validTeams()[newTeam.currentIndex].team.processes.map(function(p) { return p.name; }) : [] }
            RowLayout { Layout.fillWidth: true; Text { text: "Provider"; color: root.muted; Layout.preferredWidth: 100 } ComboBox { id: newProvider; Layout.fillWidth: true; model: root.providerChoiceLabels() } }
            CheckBox { id: newReady; text: "Create in Ready (does not start execution)" }
            RowLayout { Text { text: "Merge policy"; color: root.muted } ComboBox { id: newMergePolicy; Layout.fillWidth: true; model: ["Use project setting", "Inspect before merge", "Merge on completion"] } }
            RowLayout {
                Item { Layout.fillWidth: true }
                ActionButton { text: "Cancel"; dark: root.dark; onClicked: newTicketDialog.close() }
                ActionButton { text: "Create ticket"; primary: true; dark: root.dark; enabled: newTitle.text.trim().length > 0 && newTeam.currentIndex >= 0 && newProcess.currentIndex >= 0; onClicked: { var bundle = root.validTeams()[newTeam.currentIndex]; root.request("create", {title: newTitle.text, description: newDescription.text, team_ref: bundle.ref, process_id: bundle.team.processes[newProcess.currentIndex].id, provider_id: root.providerChoiceId(newProvider.currentIndex), epic_id: newEpicId.text.trim() || null, ready: newReady.checked, merge_auto: newMergePolicy.currentIndex === 0 ? null : newMergePolicy.currentIndex === 2, source_discussion_id: root.pendingSourceDiscussion || null}); } }
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
        contentItem: ColumnLayout {
            Input { id: editTitle; dark: root.dark; Layout.fillWidth: true }
            Input { id: editEpicId; dark: root.dark; Layout.fillWidth: true; placeholderText: "Epic ID (optional)" }
            RowLayout { Layout.fillWidth: true; Text { text: "Provider"; color: root.muted; Layout.preferredWidth: 100 } ComboBox { id: editProvider; Layout.fillWidth: true; model: root.providerChoiceLabels(); enabled: !!root.detail.ticket && root.detail.ticket.state !== "In Progress" } }
            ScrollView { Layout.fillWidth: true; Layout.preferredHeight: 250; TextArea { id: editDescription; color: root.ink; textFormat: TextEdit.PlainText; wrapMode: TextEdit.Wrap; selectByMouse: true; readOnly: !!root.detail.ticket && root.detail.ticket.state === "In Progress" } }
        }
        onAccepted: {
            var data = {ticket: root.selectedTicket, revision: root.detail.ticket.revision, title: editTitle.text, epic_id: editEpicId.text.trim()};
            if (root.detail.ticket.state !== "In Progress") {
                data.description = editDescription.text;
                data.provider_id = root.providerChoiceId(editProvider.currentIndex);

            }
            root.request("edit_ticket", data);
        }
    }
    Dialog {
        id: ticketDescriptionDialog
        objectName: "ticketDescriptionDialog"
        anchors.centerIn: parent
        width: Math.min(root.width - 120, 980)
        height: Math.min(root.height - 120, 760)
        modal: true
        title: root.detail.ticket ? root.detail.ticket.key + " — Full description" : "Ticket description"
        standardButtons: Dialog.Close
        contentItem: ScrollView {
            id: ticketDescriptionDialogScroll
            objectName: "ticketDescriptionDialogScroll"
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            TextArea {
                id: ticketDescriptionDialogText
                objectName: "ticketDescriptionDialogText"
                width: ticketDescriptionDialogScroll.availableWidth
                text: root.detail.ticket ? root.detail.ticket.description : ""
                textFormat: TextEdit.MarkdownText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
                color: root.ink
                font.pixelSize: 15
                padding: 16
                onLinkActivated: function(link) { Qt.openUrlExternally(link); }
                background: Rectangle {
                    color: root.dark ? "#101923" : "#f7f9fb"
                    radius: 7
                    border.color: root.borderColor
                }
                ChatDocumentStyle {
                    document: ticketDescriptionDialogText.textDocument
                    dark: root.dark
                }
            }
        }
    }
    Dialog {
        id: answerDialog; anchors.centerIn: parent; width: 650; modal: true; title: "Answer the paused team member"
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: ColumnLayout { RenderedText { id: questionText; Layout.fillWidth: true; sourceText: ""; dark: root.dark } ScrollView { Layout.fillWidth: true; Layout.preferredHeight: 180; TextArea { id: answerText; color: root.ink; selectByMouse: true; wrapMode: TextEdit.Wrap } } }
        onAccepted: root.request("answer", {id: root.pendingQuestion, answer: answerText.text})
    }
    Dialog {
        id: providerDialog
        anchors.centerIn: parent
        width: Math.min(root.width - 100, 760)
        modal: true
        property bool editing: false
        title: editing ? "Edit provider" : "Add provider"
        function loadProvider(provider) {
            editing = !!provider;
            providerIdField.text = provider ? provider.id : "";
            providerNameField.text = provider ? provider.name : "";
            providerEndpointField.text = provider ? provider.profile.base_url : "http://127.0.0.1:1234/v1";
            providerModelField.text = provider && provider.profile.model ? provider.profile.model : "";
            providerKeyField.text = provider && provider.profile.api_key_env ? provider.profile.api_key_env : "";
            providerTimeoutField.text = provider && provider.profile.timeout_seconds !== null && provider.profile.timeout_seconds !== undefined ? String(provider.profile.timeout_seconds) : "";
            providerReasoning.checked = provider ? !!provider.profile.round_trip_reasoning : false;
            providerOptions.text = provider ? JSON.stringify(provider.profile.request_options || {}, null, 2) : "{}";
            providerMakeDefault.checked = provider ? provider.id === root.providerPool.default_provider_id : root.providerEntries().length === 0;
            open();
        }
        contentItem: ColumnLayout {
            spacing: 11
            GridLayout {
                columns: 2
                Layout.fillWidth: true
                rowSpacing: 10
                columnSpacing: 16
                Text { text: "Provider ID"; color: root.muted; Layout.preferredWidth: 170 }
                Input { id: providerIdField; dark: root.dark; Layout.fillWidth: true; readOnly: providerDialog.editing; placeholderText: "e.g. local-qwen" }
                Text { text: "Friendly name"; color: root.muted }
                Input { id: providerNameField; dark: root.dark; Layout.fillWidth: true; placeholderText: "e.g. Desktop LM Studio — Qwen" }
                Text { text: "Base URL"; color: root.muted }
                Input { id: providerEndpointField; dark: root.dark; Layout.fillWidth: true; placeholderText: "http://127.0.0.1:1234/v1" }
                Text { text: "Model ID"; color: root.muted }
                RowLayout {
                    Layout.fillWidth: true
                    Input { id: providerModelField; dark: root.dark; Layout.fillWidth: true; placeholderText: "Blank = use the single model listed by the server" }
                    ActionButton {
                        text: "List models"; dark: root.dark
                        onClicked: {
                            try {
                                root.request("provider_models", {profile: {
                                    base_url: providerEndpointField.text.trim(),
                                    model: providerModelField.text.trim() || null,
                                    api_key_env: providerKeyField.text.trim() || null,
                                    timeout_seconds: providerTimeoutField.text.trim().length ? Number(providerTimeoutField.text) : null,
                                    request_options: JSON.parse(providerOptions.text || "{}"),
                                    round_trip_reasoning: providerReasoning.checked
                                }});
                            } catch (error) { root.statusMessage = "Invalid request options JSON: " + error; }
                        }
                    }
                }
                Text { text: "API key environment variable"; color: root.muted; wrapMode: Text.WordWrap }
                Input { id: providerKeyField; dark: root.dark; Layout.fillWidth: true; placeholderText: "Optional variable name, not the key value" }
                Text { text: "Model timeout (seconds)"; color: root.muted }
                Input { id: providerTimeoutField; dark: root.dark; Layout.fillWidth: true; inputMethodHints: Qt.ImhFormattedNumbersOnly; placeholderText: "Blank = no timeout" }
            }
            CheckBox { id: providerReasoning; text: "Round-trip provider reasoning content" }
            Text { text: "Request options (JSON)"; color: root.muted }
            ScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: 100
                TextArea { id: providerOptions; width: parent.width; color: root.ink; font.family: "monospace"; selectByMouse: true; wrapMode: TextEdit.NoWrap }
            }
            CheckBox {
                id: providerMakeDefault
                text: "Make this the default provider"
                enabled: !(providerDialog.editing && providerIdField.text === root.providerPool.default_provider_id)
                ToolTip.visible: hovered && !enabled
                ToolTip.text: "A non-empty Provider Pool always has one default. Choose another provider as default from the pool before changing this one."
            }
            RowLayout {
                Layout.fillWidth: true
                ActionButton { text: "Cancel"; dark: root.dark; onClicked: providerDialog.close() }
                Item { Layout.fillWidth: true }
                ActionButton {
                    text: "Save provider"; primary: true; dark: root.dark
                    enabled: providerIdField.text.trim().length > 0 && providerNameField.text.trim().length > 0 && providerEndpointField.text.trim().length > 0
                    onClicked: {
                        try {
                            root.request("save_provider", {provider: {
                                id: providerIdField.text.trim(),
                                name: providerNameField.text.trim(),
                                profile: {
                                    base_url: providerEndpointField.text.trim(),
                                    model: providerModelField.text.trim() || null,
                                    api_key_env: providerKeyField.text.trim() || null,
                                    timeout_seconds: providerTimeoutField.text.trim().length ? Number(providerTimeoutField.text) : null,
                                    request_options: JSON.parse(providerOptions.text || "{}"),
                                    round_trip_reasoning: providerReasoning.checked
                                }
                            }, make_default: providerMakeDefault.checked});
                        } catch (error) { root.statusMessage = "Invalid request options JSON: " + error; }
                    }
                }
            }
        }
    }

    Dialog {
        id: pmMessageDeleteDialog
        anchors.centerIn: parent; width: 480; modal: true
        title: "Delete Project Manager message?"
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Text {
            text: "Delete this message from the Project Manager conversation? Any tool results attached to it will also be removed from the conversation."
            color: root.ink; wrapMode: Text.WordWrap
        }
        onAccepted: {
            if (root.pmPendingMessageDeletionId)
                root.request("pm_delete_message", {id: root.pmPendingMessageDeletionId});
            root.pmPendingMessageDeletionId = "";
        }
        onRejected: root.pmPendingMessageDeletionId = ""
    }
    Dialog {
        id: pmClearConversationDialog
        anchors.centerIn: parent; width: 520; modal: true
        title: "Clear Project Manager conversation?"
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Text {
            text: "Permanently clear the Project Manager chat history? This also clears the model's conversation context. Saved goals, plans, settings, tickets and execution records are not changed."
            color: root.ink; wrapMode: Text.WordWrap
        }
        onAccepted: root.request("pm_clear_conversation")
    }
    Dialog {
        id: pmProfileDeleteDialog
        anchors.centerIn: parent; width: 530; modal: true
        title: "Delete global Project Manager profile?"
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Text {
            text: "Permanently delete '" + (root.currentPMProfile() ? root.currentPMProfile().name : "") +
                  "' from the global library? Other projects using it will use the default role when opened."
            color: root.ink; wrapMode: Text.WordWrap
        }
        onAccepted: if (root.pmEditingProfileId) root.request("pm_profile_delete", {id: root.pmEditingProfileId})
    }
    Dialog {
        id: advancedDialog; anchors.centerIn: parent; width: Math.min(root.width - 100, 940); height: root.height - 140; modal: true; title: "Advanced settings — validated JSON"
        contentItem: ColumnLayout {
            Text { text: "Edit allow/ask/deny tool_rules, MCP servers, setup commands, budgets and cleanup. Provider/model configurations are managed in the Provider Pool."; wrapMode: Text.WordWrap; Layout.fillWidth: true; color: root.muted; font.pixelSize: 12 }
            ScrollView { Layout.fillWidth: true; Layout.fillHeight: true; clip: true; TextArea { id: advancedText; color: root.ink; font.family: "monospace"; font.pixelSize: 13; selectByMouse: true; wrapMode: TextEdit.NoWrap } }
            RowLayout { ActionButton { text: "Cancel"; dark: root.dark; onClicked: advancedDialog.close() } Item { Layout.fillWidth: true } ActionButton { text: "Validate & save"; primary: true; dark: root.dark; onClicked: { try { root.request("settings", {settings: JSON.parse(advancedText.text), scope: settingsScope.currentIndex === 0 ? "project" : "global"}); } catch (error) { root.statusMessage = "Invalid JSON: " + error; } } } }
        }
    }
    Dialog { id: modelsDialog; anchors.centerIn: parent; width: 580; height: 360; modal: true; title: "Models listed by the provider endpoint"; standardButtons: Dialog.Close; contentItem: ListView { model: root.models; clip: true; delegate: ItemDelegate { required property string modelData; width: ListView.view.width; text: modelData; onClicked: { providerModelField.text = modelData; modelsDialog.close(); root.statusMessage = "Model selected in the provider form. Save the provider to apply."; } } } }
    Dialog { id: mergeDialog; anchors.centerIn: parent; width: 580; modal: true; title: "Apply this integration candidate?"; standardButtons: Dialog.Yes | Dialog.No; contentItem: Text { text: "Merge the inspected candidate into " + (root.pendingMerge.target_branch || "") + "?\n\nAgileCode rechecks both commit IDs and requires a clean original checkout. Worktree/checkpoint cleanup follows the configured policy."; color: root.ink; wrapMode: Text.WordWrap }
        onAccepted: root.request("merge", {id: root.pendingMerge.id, candidate: root.pendingMerge.candidate_oid, target: root.pendingMerge.target_oid}) }
    Dialog { id: cancelDialog; anchors.centerIn: parent; width: 560; modal: true; title: "Cancel this ticket run?"; standardButtons: Dialog.Yes | Dialog.No; contentItem: Text { text: "Stop this run and its owned processes? Source, checkpoints and history remain. Interrupted external effects may need reconciliation before work resumes."; color: root.ink; wrapMode: Text.WordWrap }
        onAccepted: root.request("cancel", {ticket: root.selectedTicket}) }
    Dialog {
        id: deleteAttemptDialog
        objectName: "deleteAttemptDialog"
        anchors.centerIn: parent
        width: Math.min(620, root.width - 80)
        modal: true
        title: "Delete attempt and rewind ticket?"
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Text {
            text: "Delete " + (root.pendingDeleteAttempt.step || "selected step") +
                  " · attempt " + (root.pendingDeleteAttempt.number || "") +
                  " and every subsequent attempt? Their conversations, outputs, diffs, checkpoints and associated work will be discarded. " +
                  "AgileCode will pause the ticket, restore the worktree files to the state before the selected attempt, " +
                  "and leave that step ready to run again. External effects outside the worktree cannot be undone. " +
                  "Earlier attempts remain intact. This cannot be undone."
            color: root.ink
            wrapMode: Text.WordWrap
        }
        onAccepted: {
            if (root.pendingDeleteAttempt.id && root.selectedTicket)
                root.request("delete_attempt", {ticket: root.selectedTicket, attempt: root.pendingDeleteAttempt.id});
        }
    }
    Dialog {
        id: deleteTicketDialog
        objectName: "deleteTicketDialog"
        anchors.centerIn: parent
        width: 620
        modal: true
        title: root.detail.ticket ? "Permanently delete " + root.detail.ticket.key + "?" : "Permanently delete ticket?"
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Text {
            text: "This is destructive and cannot be undone. AgileCode will stop the ticket, discard its uncommitted work, and permanently remove its worktrees, branch, checkpoints, integration state, artifacts, conversations, execution history, and ticket record. Other tickets are not modified."
            color: root.ink
            wrapMode: Text.WordWrap
        }
        onAccepted: root.request("delete_ticket", {ticket: root.selectedTicket})
    }
    Dialog {
        id: restoreDialog; anchors.centerIn: parent; width: 700; height: Math.min(650, root.height - 100); modal: true; title: "Inspect checkpoint restoration"
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: ScrollView { TextArea { id: restoreText; color: root.ink; font.family: "monospace"; font.pixelSize: 12; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap } }
        onAccepted: root.request("restore_checkpoint", {ticket: root.selectedTicket, id: root.pendingRestore.checkpoint_id, expected_tree: root.pendingRestore.expected_tree})
    }
    Dialog {
        id: pathDialog; property string operation: "backup"; anchors.centerIn: parent; width: 680; modal: true; title: operation === "backup" ? "Create portable project backup" : "Export ticket history"
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: ColumnLayout { Text { text: "Choose a new output path. Existing files are never overwritten. Backups require an idle engine and a clean original checkout."; color: root.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true } Input { id: pathField; dark: root.dark; Layout.fillWidth: true } }
        onAccepted: root.request(operation, {ticket: root.selectedTicket, path: pathField.text})
    }
    Dialog { id: errorDialog; anchors.centerIn: parent; width: Math.min(680, root.width - 80); modal: true; title: "Action needs attention"; standardButtons: Dialog.Ok; contentItem: Text { id: errorText; color: root.ink; wrapMode: Text.WrapAnywhere; font.pixelSize: 14 } }
    Dialog { id: quitDialog; anchors.centerIn: parent; width: 600; modal: true; title: "Close AgileCode?"; standardButtons: Dialog.Yes | Dialog.No; contentItem: Text { text: "Closing this host stops model requests and owned shells. Completed steps and conversations are retained. Shell environment state will not survive. Interrupted commands are recorded in ticket conversations and may have partial effects."; color: root.ink; wrapMode: Text.WordWrap }
        onAccepted: { root.closingConfirmed = true; root.close(); } }
}
