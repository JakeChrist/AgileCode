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
    property string liveText: ""
    property string liveReasoning: ""
    property var liveToolCalls: []
    property string liveRequestId: ""
    property bool followTail: true
    readonly property bool hasLiveMessage: liveText.length > 0 || liveReasoning.length > 0 || liveToolCalls.length > 0
    clip: true
    spacing: 8
    boundsBehavior: Flickable.StopAtBounds
    model: messageModel
    ScrollBar.vertical: ScrollBar { id: conversationScroll }
    ListModel { id: messageModel; dynamicRoles: true }

    function resetLive() {
        liveText = "";
        liveReasoning = "";
        liveToolCalls = [];
        liveRequestId = "";
    }
    function appendDelta(event) {
        if (!sessionId || event.session_id !== sessionId) return;
        var requestId = event.data.request_id;
        // The bridge may flush coalesced deltas after the persisted snapshot.
        if (messages.some(function(row) { return row.body.role === "assistant" && row.request_id === requestId; })) return;
        if (liveRequestId !== requestId) {
            resetLive();
            liveRequestId = requestId;
        }
        if (event.kind === "ModelTextDelta") liveText += event.data.value;
        else if (event.kind === "ModelReasoningDelta") liveReasoning += event.data.value;
        else if (event.kind === "ModelToolDelta") {
            var fragment = event.data.value;
            var calls = liveToolCalls.slice();
            var index = fragment.index;
            var call = calls[index] || {id: "", function: {name: "", arguments: ""}};
            if (fragment.id) call.id = fragment.id;
            var fn = fragment.function || {};
            if (fn.name && call.function.name !== fn.name) call.function.name += fn.name;
            if (fn.arguments) call.function.arguments += typeof fn.arguments === "string" ? fn.arguments : JSON.stringify(fn.arguments);
            calls[index] = call;
            liveToolCalls = calls;
        }
    }
    function updateMessages() {
        var rows = [];
        var consumed = ({});
        for (var i = 0; i < messages.length; ++i) {
            var record = messages[i];
            if (consumed[i]) continue;
            var results = ({});
            if (record.body.role === "assistant") {
                var calls = record.body.tool_calls || [];
                // Call IDs may be reused on later turns: only use adjacent results.
                for (var j = i + 1; j < messages.length && messages[j].body.role === "tool"; ++j) {
                    var result = messages[j].body;
                    if (calls.some(function(call) { return call.id === result.tool_call_id; })) {
                        results[result.tool_call_id] = result;
                        consumed[j] = true;
                    }
                }
            }
            rows.push({recordId: record.id || String(record.seq), messageBody: record.body, results: results, interrupted: !!record.partial});
        }
        // Keep existing delegates (and disclosure state) when history is refreshed.
        for (var n = 0; n < rows.length; ++n) {
            if (n < messageModel.count && messageModel.get(n).recordId !== rows[n].recordId)
                messageModel.remove(n, messageModel.count - n);
            if (n === messageModel.count) messageModel.append(rows[n]);
            else {
                ["messageBody", "results", "interrupted"].forEach(function(key) {
                    if (JSON.stringify(messageModel.get(n)[key]) !== JSON.stringify(rows[n][key]))
                        messageModel.setProperty(n, key, rows[n][key]);
                });
            }
        }
        if (messageModel.count > rows.length) messageModel.remove(rows.length, messageModel.count - rows.length);
        // A snapshot for an earlier turn must never erase the next streamed turn.
        if (liveRequestId && messages.some(function(row) { return row.body.role === "assistant" && row.request_id === liveRequestId; })) resetLive();
    }
    function scrollIfFollowing() {
        if (followTail && !moving && !conversationScroll.pressed)
            Qt.callLater(function() { if (conversation.followTail) conversation.positionViewAtEnd(); });
    }
    onMessagesChanged: updateMessages()
    onSessionIdChanged: {
        resetLive();
        messageModel.clear();
        followTail = true;
    }
    onContentHeightChanged: scrollIfFollowing()
    onHeightChanged: scrollIfFollowing()
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
        objectName: "chatMessage-" + recordId
        width: conversation.width
        body: messageBody
        toolResults: results
        partial: interrupted
        dark: conversation.dark
        assistantName: conversation.assistantName
    }
    footer: ChatMessage {
        objectName: "streamingMessage"
        width: conversation.width
        height: visible ? implicitHeight : 0
        visible: conversation.hasLiveMessage
        body: ({role: "assistant", content: conversation.liveText, reasoning_content: conversation.liveReasoning,
                tool_calls: conversation.liveToolCalls.filter(function(call) { return !!call; })})
        streaming: !conversation.interrupted
        partial: conversation.interrupted
        dark: conversation.dark
        assistantName: conversation.assistantName
        onHeightChanged: conversation.scrollIfFollowing()
    }
}
