pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls

ListView {
    id: conversation
    objectName: "conversationView"
    property string sessionId: ""
    property var messages: []
    property bool dark: true
    property string assistantName: "Assistant"
    property bool interrupted: false
    property bool reasoningExpandedByDefault: false
    property string liveText: ""
    property string liveReasoning: ""
    property var liveToolCalls: []
    property string liveRequestId: ""
    property bool followTail: true
    property bool liveReasoningExpanded: false
    readonly property bool hasLiveMessage: liveText.length > 0 || liveReasoning.length > 0 || liveToolCalls.length > 0
    clip: true
    spacing: 8
    boundsBehavior: Flickable.StopAtBounds
    model: messageModel
    ScrollBar.vertical: ScrollBar { id: conversationScroll }
    ListModel { id: messageModel; dynamicRoles: true }

    function liveRowIndex() {
        for (var i = 0; i < messageModel.count; ++i)
            if (messageModel.get(i).recordId === "__streaming__") return i;
        return -1;
    }

    function liveBody() {
        return {
            role: "assistant",
            content: liveText,
            reasoning_content: liveReasoning,
            tool_calls: liveToolCalls.filter(function(call) { return !!call; })
        };
    }

    function syncLiveRow() {
        var index = liveRowIndex();
        if (!hasLiveMessage) {
            if (index >= 0) messageModel.remove(index);
            return;
        }
        var row = {
            recordId: "__streaming__",
            messageBody: liveBody(),
            results: ({}),
            interrupted: interrupted,
            isStreaming: true
        };
        if (index < 0) {
            messageModel.append(row);
        } else {
            messageModel.setProperty(index, "messageBody", row.messageBody);
            messageModel.setProperty(index, "interrupted", row.interrupted);
            messageModel.setProperty(index, "isStreaming", true);
        }
        scheduleTailScroll();
    }

    function resetLive() {
        var index = liveRowIndex();
        if (index >= 0) messageModel.remove(index);
        liveText = "";
        liveReasoning = "";
        liveToolCalls = [];
        liveRequestId = "";
        liveReasoningExpanded = reasoningExpandedByDefault;
    }

    function appendDelta(event) {
        if (!sessionId || event.session_id !== sessionId) return;
        var requestId = event.data.request_id;
        // The bridge may flush a persisted snapshot before a final queued delta.
        if (messages.some(function(row) { return row.body.role === "assistant" && row.request_id === requestId; })) return;
        if (liveRequestId !== requestId) {
            resetLive();
            liveRequestId = requestId;
        }
        if (event.kind === "ModelTextDelta") {
            var answerStarting = liveText.length === 0 && liveReasoning.length > 0;
            if (answerStarting && liveReasoningExpanded) {
                followTail = false;
                tailScrollTimer.stop();
            }
            liveText += event.data.value;
        } else if (event.kind === "ModelReasoningDelta") liveReasoning += event.data.value;
        else if (event.kind === "ModelToolDelta") {
            var fragment = event.data.value;
            var calls = liveToolCalls.slice();
            var callIndex = fragment.index;
            var call = calls[callIndex] || {id: "", function: {name: "", arguments: ""}};
            if (fragment.id) call.id = fragment.id;
            var fn = fragment.function || {};
            if (fn.name && call.function.name !== fn.name) call.function.name += fn.name;
            if (fn.arguments) call.function.arguments += typeof fn.arguments === "string" ? fn.arguments : JSON.stringify(fn.arguments);
            calls[callIndex] = call;
            liveToolCalls = calls;
        }
        syncLiveRow();
    }

    function updateMessages() {
        // The live response is a normal final list row, never a resizing ListView footer.
        // Remove it only while reconciling persisted history, then restore it if the
        // persisted snapshot does not contain the current request yet.
        var liveIndex = liveRowIndex();
        if (liveIndex >= 0) messageModel.remove(liveIndex);

        var rows = [];
        var consumed = ({});
        for (var i = 0; i < messages.length; ++i) {
            var record = messages[i];
            if (consumed[i]) continue;
            var results = ({});
            if (record.body.role === "assistant") {
                var calls = record.body.tool_calls || [];
                for (var j = i + 1; j < messages.length && messages[j].body.role === "tool"; ++j) {
                    var result = messages[j].body;
                    if (calls.some(function(call) { return call.id === result.tool_call_id; })) {
                        results[result.tool_call_id] = result;
                        consumed[j] = true;
                    }
                }
            }
            rows.push({
                recordId: record.id || String(record.seq),
                messageBody: record.body,
                results: results,
                interrupted: !!record.partial,
                isStreaming: false
            });
        }

        // Keep delegates stable whenever the persisted identity is unchanged.
        for (var n = 0; n < rows.length; ++n) {
            if (n < messageModel.count && messageModel.get(n).recordId !== rows[n].recordId)
                messageModel.remove(n, messageModel.count - n);
            if (n === messageModel.count) messageModel.append(rows[n]);
            else {
                ["messageBody", "results", "interrupted", "isStreaming"].forEach(function(key) {
                    if (JSON.stringify(messageModel.get(n)[key]) !== JSON.stringify(rows[n][key]))
                        messageModel.setProperty(n, key, rows[n][key]);
                });
            }
        }
        if (messageModel.count > rows.length)
            messageModel.remove(rows.length, messageModel.count - rows.length);

        if (liveRequestId && messages.some(function(row) {
            return row.body.role === "assistant" && row.request_id === liveRequestId;
        })) {
            liveText = "";
            liveReasoning = "";
            liveToolCalls = [];
            liveRequestId = "";
        }
        syncLiveRow();
    }

    function scheduleTailScroll() {
        if (followTail && !moving && !conversationScroll.pressed)
            tailScrollTimer.restart();
    }

    function scrollToTail() {
        if (!followTail || moving || conversationScroll.pressed) return;
        var bottom = originY + Math.max(0, contentHeight - height);
        contentY = bottom;
    }

    function jumpToTail() {
        followTail = true;
        tailScrollTimer.restart();
    }

    Timer {
        id: tailScrollTimer
        interval: 48
        repeat: false
        onTriggered: conversation.scrollToTail()
    }

    onMessagesChanged: updateMessages()
    onInterruptedChanged: syncLiveRow()
    onSessionIdChanged: {
        resetLive();
        messageModel.clear();
        followTail = true;
    }
    onContentHeightChanged: scheduleTailScroll()
    onHeightChanged: scheduleTailScroll()
    onContentYChanged: {
        if (moving || conversationScroll.pressed)
            followTail = contentY + height >= originY + contentHeight - 24;
    }
    onMovementEnded: followTail = contentY + height >= originY + contentHeight - 24

    delegate: ChatMessage {
        required property string recordId
        required property var messageBody
        required property var results
        required property bool interrupted
        required property bool isStreaming
        objectName: recordId === "__streaming__" ? "streamingMessage" : "chatMessage-" + recordId
        width: conversation.width
        body: messageBody
        toolResults: results
        partial: interrupted
        streaming: isStreaming
        dark: conversation.dark
        reasoningExpandedByDefault: conversation.reasoningExpandedByDefault
        assistantName: conversation.assistantName
        onReasoningToggled: function(expanded) {
            if (recordId !== "__streaming__") return;
            conversation.liveReasoningExpanded = expanded;
            if (!expanded) {
                conversation.followTail = conversation.contentY + conversation.height >=
                                          conversation.originY + conversation.contentHeight - 24;
                conversation.scheduleTailScroll();
            }
        }
    }
}
