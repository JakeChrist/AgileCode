pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: manager
    property var teams: []
    property var availableTools: defaultTools().map(function(name) { return {name: name, label: name}; })
    property bool dark: true
    property color ink: dark ? "#e3ebf3" : "#223647"
    property color muted: dark ? "#94aabe" : "#607789"
    property color panel: dark ? "#16232f" : "#ffffff"
    property color borderColor: dark ? "#2c4152" : "#d9e2ea"
    property var draft: null
    property string selectedRef: ""
    property string selectedScope: ""
    property string selectedMemberId: ""
    property string selectedProcessId: ""
    property string selectedStepId: ""
    property int editorMode: 0 // 0 team, 1 member, 2 process, 3 step
    signal saveRequested(var team, string scope)
    signal discoverToolsRequested()

    function selectableTools() {
        var tools = availableTools.slice();
        var member = currentMember();
        if (member) member.allowed_tools.forEach(function(name) {
            if (!tools.some(function(tool) { return tool.name === name; }))
                tools.push({name: name, label: name + " (not currently discovered)"});
        });
        return tools;
    }

    function toggleTool(name, checked) {
        var member = currentMember();
        if (!member) return;
        var selected = member.allowed_tools.filter(function(tool) { return tool !== name; });
        if (checked) selected.push(name);
        updateMember("allowed_tools", selected);
    }

    onTeamsChanged: {
        if (!draft) {
            var available = teams.filter(function(item) { return item.team !== undefined; });
            if (available.length) selectTeam(available[0]);
        }
    }

    function copy(value) {
        return value === null || value === undefined ? value : JSON.parse(JSON.stringify(value));
    }

    function defaultTools() {
        return [
            "list_files", "read_file", "code_search",
            "write_file", "edit_file", "move_path", "delete_path",
            "run_command", "read_job_output", "stop_job", "read_artifact",
            "git_command", "load_skill",
            "update_progress", "record_check", "ask_user", "finish_step",
            "final_commit", "mcp_read_resource"
        ];
    }

    function selectTeam(item) {
        draft = copy(item.team);
        selectedRef = item.ref;
        selectedScope = item.scope;
        selectedMemberId = draft.members.length ? draft.members[0].id : "";
        selectedProcessId = draft.default_process_id || (draft.processes.length ? draft.processes[0].id : "");
        var process = currentProcess();
        selectedStepId = process && process.steps.length ? process.steps[0].id : "";
        editorMode = 0;
    }

    function newTeam() {
        var suffix = String(Date.now()).slice(-6);
        draft = {
            format_version: 2,
            id: "team-" + suffix,
            name: "New Team",
            members: [{
                id: "member-1",
                name: "Member 1",
                system_prompt: "Describe this member's role and responsibilities.",
                allowed_tools: defaultTools(),
                skills: [],
                model_override: null
            }],
            processes: [{
                id: "process-1",
                name: "Process 1",
                steps: [{
                    id: "step-1",
                    name: "Step 1",
                    team_member_id: "member-1",
                    prompt: "Describe what this step should accomplish.",
                    kind: "analysis",
                    correction_target: null
                }],
                standard: false
            }],
            default_process_id: "process-1"
        };
        selectedRef = "";
        selectedScope = "new";
        selectedMemberId = "member-1";
        selectedProcessId = "process-1";
        selectedStepId = "step-1";
        editorMode = 0;
    }

    function memberIndex() {
        if (!draft) return -1;
        return draft.members.findIndex(function(item) { return item.id === selectedMemberId; });
    }
    function processIndex() {
        if (!draft) return -1;
        return draft.processes.findIndex(function(item) { return item.id === selectedProcessId; });
    }
    function stepIndex() {
        var process = currentProcess();
        if (!process) return -1;
        return process.steps.findIndex(function(item) { return item.id === selectedStepId; });
    }
    function currentMember() {
        var index = memberIndex();
        return index >= 0 ? draft.members[index] : null;
    }
    function currentProcess() {
        var index = processIndex();
        return index >= 0 ? draft.processes[index] : null;
    }
    function currentStep() {
        var process = currentProcess();
        var index = stepIndex();
        return process && index >= 0 ? process.steps[index] : null;
    }
    function refresh(value) {
        draft = copy(value);
    }
    function updateTeam(field, value) {
        if (!draft) return;
        var valueCopy = copy(draft);
        valueCopy[field] = value;
        refresh(valueCopy);
    }
    function updateMember(field, value) {
        var index = memberIndex();
        if (index < 0) return;
        var valueCopy = copy(draft);
        valueCopy.members[index][field] = value;
        if (field === "id") {
            var oldId = selectedMemberId;
            valueCopy.processes.forEach(function(process) {
                process.steps.forEach(function(step) {
                    if (step.team_member_id === oldId) step.team_member_id = value;
                });
            });
            selectedMemberId = value;
        }
        refresh(valueCopy);
    }
    function updateProcess(field, value) {
        var index = processIndex();
        if (index < 0) return;
        var valueCopy = copy(draft);
        valueCopy.processes[index][field] = value;
        if (field === "id") {
            if (valueCopy.default_process_id === selectedProcessId) valueCopy.default_process_id = value;
            selectedProcessId = value;
        }
        refresh(valueCopy);
    }
    function updateStep(field, value) {
        var p = processIndex();
        var s = stepIndex();
        if (p < 0 || s < 0) return;
        var valueCopy = copy(draft);
        valueCopy.processes[p].steps[s][field] = value;
        if (field === "id") selectedStepId = value;
        refresh(valueCopy);
    }

    function addMember() {
        var valueCopy = copy(draft);
        var n = valueCopy.members.length + 1;
        var id = "member-" + n;
        while (valueCopy.members.some(function(item) { return item.id === id; })) { n += 1; id = "member-" + n; }
        valueCopy.members.push({
            id: id, name: "Member " + n,
            system_prompt: "Describe this member's role and responsibilities.",
            allowed_tools: defaultTools(), skills: [], model_override: null
        });
        selectedMemberId = id;
        editorMode = 1;
        refresh(valueCopy);
    }
    function memberReferenced(id) {
        if (!draft) return false;
        return draft.processes.some(function(process) {
            return process.steps.some(function(step) { return step.team_member_id === id; });
        });
    }
    function removeMember() {
        var index = memberIndex();
        if (index < 0 || draft.members.length <= 1 || memberReferenced(selectedMemberId)) return;
        var valueCopy = copy(draft);
        valueCopy.members.splice(index, 1);
        selectedMemberId = valueCopy.members[Math.max(0, index - 1)].id;
        editorMode = 1;
        refresh(valueCopy);
    }

    function addProcess() {
        var valueCopy = copy(draft);
        var n = valueCopy.processes.length + 1;
        var id = "process-" + n;
        while (valueCopy.processes.some(function(item) { return item.id === id; })) { n += 1; id = "process-" + n; }
        var member = valueCopy.members[0];
        valueCopy.processes.push({
            id: id,
            name: "Process " + n,
            steps: [{
                id: "step-1",
                name: "Step 1",
                team_member_id: member.id,
                prompt: "Describe what this step should accomplish.",
                kind: "analysis",
                correction_target: null
            }],
            standard: false
        });
        selectedProcessId = id;
        selectedStepId = "step-1";
        editorMode = 2;
        refresh(valueCopy);
    }
    function removeProcess() {
        var index = processIndex();
        if (index < 0 || draft.processes.length <= 1) return;
        var valueCopy = copy(draft);
        var removed = valueCopy.processes[index].id;
        valueCopy.processes.splice(index, 1);
        if (valueCopy.default_process_id === removed) valueCopy.default_process_id = valueCopy.processes[0].id;
        selectedProcessId = valueCopy.processes[Math.max(0, index - 1)].id;
        selectedStepId = valueCopy.processes[Math.max(0, index - 1)].steps[0].id;
        editorMode = 2;
        refresh(valueCopy);
    }

    function addStep() {
        var p = processIndex();
        if (p < 0) return;
        var valueCopy = copy(draft);
        var steps = valueCopy.processes[p].steps;
        var n = steps.length + 1;
        var id = "step-" + n;
        while (steps.some(function(item) { return item.id === id; })) { n += 1; id = "step-" + n; }
        steps.push({
            id: id,
            name: "Step " + n,
            team_member_id: valueCopy.members[0].id,
            prompt: "Describe what this step should accomplish.",
            kind: "analysis",
            correction_target: null
        });
        valueCopy.processes[p].standard = false;
        selectedStepId = id;
        editorMode = 3;
        refresh(valueCopy);
    }
    function removeStep() {
        var p = processIndex();
        var s = stepIndex();
        if (p < 0 || s < 0 || draft.processes[p].steps.length <= 1) return;
        var valueCopy = copy(draft);
        var removed = valueCopy.processes[p].steps[s].id;
        valueCopy.processes[p].steps.splice(s, 1);
        valueCopy.processes[p].steps.forEach(function(step) {
            if (step.correction_target === removed) step.correction_target = null;
        });
        valueCopy.processes[p].standard = false;
        var next = Math.max(0, s - 1);
        selectedStepId = valueCopy.processes[p].steps[next].id;
        editorMode = 3;
        refresh(valueCopy);
    }
    function moveStep(delta) {
        var p = processIndex();
        var s = stepIndex();
        var target = s + delta;
        if (p < 0 || s < 0 || target < 0 || target >= draft.processes[p].steps.length) return;
        var valueCopy = copy(draft);
        var steps = valueCopy.processes[p].steps;
        var moved = steps.splice(s, 1)[0];
        steps.splice(target, 0, moved);
        valueCopy.processes[p].standard = false;
        refresh(valueCopy);
    }
    function correctionOptions() {
        var process = currentProcess();
        var index = stepIndex();
        if (!process || index <= 0) return ["None"];
        return ["None"].concat(process.steps.slice(0, index).map(function(step) { return step.id; }));
    }
    function parseList(text) {
        return text.split(",").map(function(value) { return value.trim(); }).filter(function(value) { return value.length > 0; });
    }

    RowLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 18

        ColumnLayout {
            Layout.preferredWidth: 245
            Layout.minimumWidth: 220
            Layout.fillHeight: true
            spacing: 8

            RowLayout {
                Layout.fillWidth: true
                Text { text: "Teams"; color: manager.ink; font.pixelSize: 25; font.weight: Font.DemiBold }
                Item { Layout.fillWidth: true }
                ActionButton { text: "+ New"; dark: manager.dark; onClicked: manager.newTeam() }
            }
            Text { text: "Select a team to edit or clone."; color: manager.muted; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }

            ListView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: 5
                model: manager.teams.filter(function(item) { return item.team !== undefined; })
                delegate: ItemDelegate {
                    required property var modelData
                    width: ListView.view.width
                    height: 62
                    highlighted: manager.selectedRef === modelData.ref
                    contentItem: Column {
                        spacing: 4
                        Text { width: parent.width; text: modelData.team.name; color: manager.ink; elide: Text.ElideRight; font.pixelSize: 13; font.weight: Font.DemiBold }
                        Text { text: modelData.scope.toUpperCase() + " · " + modelData.team.members.length + " members · " + modelData.team.processes.length + " processes"; color: manager.muted; font.pixelSize: 10 }
                    }
                    background: Rectangle { color: parent.highlighted ? (manager.dark ? "#253b49" : "#e3edf5") : (parent.hovered ? (manager.dark ? "#1d303e" : "#eef3f7") : "transparent"); radius: 7 }
                    onClicked: manager.selectTeam(modelData)
                }
            }
        }

        Rectangle { Layout.fillHeight: true; width: 1; color: manager.borderColor }

        ColumnLayout {
            Layout.preferredWidth: 310
            Layout.minimumWidth: 270
            Layout.fillHeight: true
            spacing: 9

            Text { text: manager.draft ? manager.draft.name : "Team structure"; color: manager.ink; font.pixelSize: 20; font.weight: Font.DemiBold; Layout.fillWidth: true; elide: Text.ElideRight }
            ActionButton { text: "Team settings"; dark: manager.dark; Layout.fillWidth: true; onClicked: manager.editorMode = 0 }

            RowLayout {
                Layout.fillWidth: true
                Text { text: "MEMBERS"; color: manager.muted; font.pixelSize: 10; font.letterSpacing: 1 }
                Item { Layout.fillWidth: true }
                ActionButton { text: "+"; dark: manager.dark; enabled: !!manager.draft; onClicked: manager.addMember() }
            }
            ListView {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(210, contentHeight)
                clip: true
                spacing: 3
                model: manager.draft ? manager.draft.members : []
                delegate: ItemDelegate {
                    required property var modelData
                    width: ListView.view.width
                    height: 42
                    highlighted: manager.selectedMemberId === modelData.id && manager.editorMode === 1
                    text: modelData.name
                    onClicked: { manager.selectedMemberId = modelData.id; manager.editorMode = 1; }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Text { text: "PROCESSES"; color: manager.muted; font.pixelSize: 10; font.letterSpacing: 1 }
                Item { Layout.fillWidth: true }
                ActionButton { text: "+"; dark: manager.dark; enabled: !!manager.draft; onClicked: manager.addProcess() }
            }
            ListView {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(180, contentHeight)
                clip: true
                spacing: 3
                model: manager.draft ? manager.draft.processes : []
                delegate: ItemDelegate {
                    required property var modelData
                    width: ListView.view.width
                    height: 42
                    highlighted: manager.selectedProcessId === modelData.id && manager.editorMode === 2
                    text: modelData.name
                    onClicked: {
                        manager.selectedProcessId = modelData.id;
                        manager.selectedStepId = modelData.steps.length ? modelData.steps[0].id : "";
                        manager.editorMode = 2;
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                visible: !!manager.currentProcess()
                Text { text: "STEPS"; color: manager.muted; font.pixelSize: 10; font.letterSpacing: 1 }
                Item { Layout.fillWidth: true }
                ActionButton { text: "+"; dark: manager.dark; onClicked: manager.addStep() }
            }
            ListView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: 3
                model: manager.currentProcess() ? manager.currentProcess().steps : []
                delegate: ItemDelegate {
                    required property var modelData
                    required property int index
                    width: ListView.view.width
                    height: 46
                    highlighted: manager.selectedStepId === modelData.id && manager.editorMode === 3
                    contentItem: RowLayout {
                        Text { text: (index + 1) + "."; color: manager.muted; font.pixelSize: 11 }
                        Text { text: modelData.name; color: manager.ink; Layout.fillWidth: true; elide: Text.ElideRight }
                        Text { text: modelData.kind; color: manager.muted; font.pixelSize: 10 }
                    }
                    onClicked: { manager.selectedStepId = modelData.id; manager.editorMode = 3; }
                }
            }
        }

        Rectangle { Layout.fillHeight: true; width: 1; color: manager.borderColor }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 10

            Text {
                text: manager.editorMode === 0 ? "Team settings" : manager.editorMode === 1 ? "Member" : manager.editorMode === 2 ? "Process" : "Step"
                color: manager.ink
                font.pixelSize: 21
                font.weight: Font.DemiBold
            }
            Text {
                visible: manager.selectedScope === "builtin"
                text: "Built-in teams are immutable. Saving creates an editable global or project copy."
                color: manager.muted
                font.pixelSize: 11
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: manager.editorMode

                ScrollView {
                    clip: true
                    ColumnLayout {
                        width: parent.width
                        spacing: 11
                        Text { text: "Team name"; color: manager.muted }
                        Input {
                            Layout.fillWidth: true; dark: manager.dark
                            text: manager.draft ? manager.draft.name : ""
                            enabled: !!manager.draft
                            onEditingFinished: manager.updateTeam("name", text.trim())
                        }
                        Text { text: "Team ID"; color: manager.muted }
                        Input {
                            Layout.fillWidth: true; dark: manager.dark
                            text: manager.draft ? manager.draft.id : ""
                            enabled: !!manager.draft
                            onEditingFinished: manager.updateTeam("id", text.trim())
                        }
                        Text { text: "Default process"; color: manager.muted }
                        ComboBox {
                            Layout.fillWidth: true
                            enabled: !!manager.draft
                            model: manager.draft ? manager.draft.processes.map(function(item) { return item.name; }) : []
                            currentIndex: manager.draft ? Math.max(0, manager.draft.processes.findIndex(function(item) { return item.id === manager.draft.default_process_id; })) : -1
                            onActivated: if (manager.draft && currentIndex >= 0) manager.updateTeam("default_process_id", manager.draft.processes[currentIndex].id)
                        }
                    }
                }

                ScrollView {
                    clip: true
                    ColumnLayout {
                        id: memberEditor
                        width: parent.width
                        spacing: 11
                        property var member: manager.currentMember()
                        Text { text: "Name"; color: manager.muted }
                        Input {
                            Layout.fillWidth: true; dark: manager.dark
                            text: memberEditor.member ? memberEditor.member.name : ""
                            onEditingFinished: manager.updateMember("name", text.trim())
                        }
                        Text { text: "Member ID"; color: manager.muted }
                        Input {
                            Layout.fillWidth: true; dark: manager.dark
                            text: memberEditor.member ? memberEditor.member.id : ""
                            onEditingFinished: manager.updateMember("id", text.trim())
                        }
                        Text { text: "Provider override ID"; color: manager.muted }
                        Input {
                            Layout.fillWidth: true; dark: manager.dark
                            text: memberEditor.member && memberEditor.member.model_override ? memberEditor.member.model_override : ""
                            placeholderText: "Blank = ticket/default provider"
                            onEditingFinished: manager.updateMember("model_override", text.trim().length ? text.trim() : null)
                        }
                        Text { text: "Allowed tools"; color: manager.muted }
                        RowLayout {
                            ActionButton { text: "Select all"; dark: manager.dark; onClicked: manager.updateMember("allowed_tools", manager.selectableTools().map(function(tool) { return tool.name; })) }
                            ActionButton { text: "Clear"; dark: manager.dark; onClicked: manager.updateMember("allowed_tools", []) }
                            ActionButton { text: "Discover MCP tools"; dark: manager.dark; onClicked: manager.discoverToolsRequested() }
                        }
                        Flow {
                            Layout.fillWidth: true
                            spacing: 6
                            Repeater {
                                model: manager.selectableTools()
                                delegate: CheckBox {
                                    required property var modelData
                                    text: modelData.label
                                    checked: memberEditor.member ? memberEditor.member.allowed_tools.indexOf(modelData.name) >= 0 : false
                                    onClicked: manager.toggleTool(modelData.name, checked)
                                }
                            }
                        }
                        Input {
                            Layout.fillWidth: true; dark: manager.dark
                            text: memberEditor.member ? memberEditor.member.allowed_tools.join(", ") : ""
                            placeholderText: "Comma-separated tool names, e.g. read_file, run_command"
                            onEditingFinished: manager.updateMember("allowed_tools", manager.parseList(text))
                        }
                        Text { text: "Skills"; color: manager.muted }
                        Input {
                            Layout.fillWidth: true; dark: manager.dark
                            text: memberEditor.member ? memberEditor.member.skills.join(", ") : ""
                            placeholderText: "Comma-separated skill IDs"
                            onEditingFinished: manager.updateMember("skills", manager.parseList(text))
                        }
                        Text { text: "System prompt"; color: manager.muted }
                        ScrollView {
                            Layout.fillWidth: true; Layout.preferredHeight: 230; clip: true
                            TextArea {
                                width: parent.width
                                text: memberEditor.member ? memberEditor.member.system_prompt : ""
                                selectByMouse: true
                                wrapMode: TextEdit.Wrap
                                color: manager.ink
                                background: Rectangle { color: manager.dark ? "#101c27" : "#f7f9fb"; radius: 7; border.color: manager.borderColor }
                                onActiveFocusChanged: if (!activeFocus) manager.updateMember("system_prompt", text)
                            }
                        }
                        ActionButton {
                            text: manager.memberReferenced(manager.selectedMemberId) ? "Member is used by a step" : "Remove member"
                            danger: true; dark: manager.dark
                            enabled: !!manager.draft && manager.draft.members.length > 1 && !manager.memberReferenced(manager.selectedMemberId)
                            onClicked: manager.removeMember()
                        }
                    }
                }

                ScrollView {
                    clip: true
                    ColumnLayout {
                        id: processEditor
                        width: parent.width
                        spacing: 11
                        property var process: manager.currentProcess()
                        Text { text: "Process name"; color: manager.muted }
                        Input {
                            Layout.fillWidth: true; dark: manager.dark
                            text: processEditor.process ? processEditor.process.name : ""
                            onEditingFinished: manager.updateProcess("name", text.trim())
                        }
                        Text { text: "Process ID"; color: manager.muted }
                        Input {
                            Layout.fillWidth: true; dark: manager.dark
                            text: processEditor.process ? processEditor.process.id : ""
                            onEditingFinished: manager.updateProcess("id", text.trim())
                        }
                        CheckBox {
                            text: "Enforce built-in standard 8-stage structure"
                            checked: processEditor.process ? processEditor.process.standard : false
                            onToggled: if (processEditor.process && checked !== processEditor.process.standard) manager.updateProcess("standard", checked)
                        }
                        Text { text: processEditor.process ? processEditor.process.steps.length + " ordered steps" : ""; color: manager.muted }
                        ActionButton {
                            text: "Remove process"
                            danger: true; dark: manager.dark
                            enabled: !!manager.draft && manager.draft.processes.length > 1
                            onClicked: manager.removeProcess()
                        }
                    }
                }

                ScrollView {
                    clip: true
                    ColumnLayout {
                        id: stepEditor
                        width: parent.width
                        spacing: 11
                        property var step: manager.currentStep()
                        property var process: manager.currentProcess()
                        RowLayout {
                            Layout.fillWidth: true
                            ActionButton { text: "↑"; dark: manager.dark; enabled: manager.stepIndex() > 0; onClicked: manager.moveStep(-1) }
                            ActionButton { text: "↓"; dark: manager.dark; enabled: stepEditor.step && stepEditor.process && manager.stepIndex() < stepEditor.process.steps.length - 1; onClicked: manager.moveStep(1) }
                            Item { Layout.fillWidth: true }
                            ActionButton { text: "Remove step"; danger: true; dark: manager.dark; enabled: stepEditor.process && stepEditor.process.steps.length > 1; onClicked: manager.removeStep() }
                        }
                        Text { text: "Step name"; color: manager.muted }
                        Input {
                            Layout.fillWidth: true; dark: manager.dark
                            text: stepEditor.step ? stepEditor.step.name : ""
                            onEditingFinished: manager.updateStep("name", text.trim())
                        }
                        Text { text: "Step ID"; color: manager.muted }
                        Input {
                            Layout.fillWidth: true; dark: manager.dark
                            text: stepEditor.step ? stepEditor.step.id : ""
                            onEditingFinished: manager.updateStep("id", text.trim())
                        }
                        Text { text: "Kind"; color: manager.muted }
                        ComboBox {
                            Layout.fillWidth: true
                            property var kinds: ["understand", "requirements", "architecture", "plan", "implement", "verify", "review", "commit", "analysis"]
                            model: kinds
                            currentIndex: stepEditor.step ? kinds.indexOf(stepEditor.step.kind) : -1
                            onActivated: manager.updateStep("kind", kinds[currentIndex])
                        }
                        Text { text: "Assigned member"; color: manager.muted }
                        ComboBox {
                            Layout.fillWidth: true
                            model: manager.draft ? manager.draft.members.map(function(item) { return item.name; }) : []
                            currentIndex: stepEditor.step && manager.draft ? Math.max(0, manager.draft.members.findIndex(function(item) { return item.id === stepEditor.step.team_member_id; })) : -1
                            onActivated: if (manager.draft && currentIndex >= 0) manager.updateStep("team_member_id", manager.draft.members[currentIndex].id)
                        }
                        Text { text: "Correction target"; color: manager.muted }
                        ComboBox {
                            Layout.fillWidth: true
                            property var choices: manager.correctionOptions()
                            model: choices
                            currentIndex: stepEditor.step && stepEditor.step.correction_target ? Math.max(0, choices.indexOf(stepEditor.step.correction_target)) : 0
                            onActivated: manager.updateStep("correction_target", currentIndex === 0 ? null : choices[currentIndex])
                        }
                        Text { text: "Step instructions"; color: manager.muted }
                        ScrollView {
                            Layout.fillWidth: true; Layout.preferredHeight: 230; clip: true
                            TextArea {
                                width: parent.width
                                text: stepEditor.step ? stepEditor.step.prompt : ""
                                selectByMouse: true
                                wrapMode: TextEdit.Wrap
                                color: manager.ink
                                background: Rectangle { color: manager.dark ? "#101c27" : "#f7f9fb"; radius: 7; border.color: manager.borderColor }
                                onActiveFocusChanged: if (!activeFocus) manager.updateStep("prompt", text)
                            }
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Text { text: "Save scope"; color: manager.muted }
                ComboBox { id: saveScope; model: ["Global", "This project"] }
                Item { Layout.fillWidth: true }
                ActionButton {
                    text: "Save team"
                    primary: true
                    dark: manager.dark
                    enabled: !!manager.draft
                    onClicked: manager.saveRequested(manager.copy(manager.draft), saveScope.currentIndex === 0 ? "global" : "project")
                }
            }
        }
    }
}
