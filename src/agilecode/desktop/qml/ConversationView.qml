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
    property var readingAnchor: null
    property bool restoringPosition: false
    readonly property bool hasLiveMessage: liveText.length > 0 || liveReasoning.length > 0 || liveToolCalls.length > 0
    clip: true
    spacing: 8
    boundsBehavior: Flickable.StopAtBounds
    model: messageModel
    ScrollBar.vertical: ScrollBar {
        id: conversationScroll
        objectName: "conversationScrollBar"
        onPressedChanged: {
            if (pressed) conversation.stopFollowing();
            else conversation.finishInteraction();
        }
    }
    WheelHandler {
        target: null
        onWheel: function(event) {
            conversation.stopFollowing();
            conversation.cancelFlick();
            var distance = event.pixelDelta.y || event.angleDelta.y / 120 * 60;
            var bottom = conversation.originY + Math.max(0, conversation.contentHeight - conversation.height);
            conversation.contentY = Math.max(conversation.originY, Math.min(bottom, conversation.contentY - distance));
            conversation.captureAnchor();
            interactionTimer.restart();
            event.accepted = true;
        }
    }
    Timer {
        id: interactionTimer
        interval: 120
        onTriggered: conversation.finishInteraction()
    }
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
            requestId: liveRequestId,
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
        captureAnchor();
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
                requestId: record.request_id || "",
                messageBody: record.body,
                results: results,
                interrupted: !!record.partial,
                isStreaming: false
            });
        }

        var savedLive = liveRequestId && messages.some(function(row) {
            return row.body.role === "assistant" && row.request_id === liveRequestId;
        });
        if (hasLiveMessage && !savedLive) {
            rows.push({recordId: "__streaming__", requestId: liveRequestId,
                       messageBody: liveBody(), results: ({}), interrupted: interrupted,
                       isStreaming: true});
        }
        // Insert older history without replacing existing delegates. Promote the
        // live row in place when its persisted assistant message arrives.
        for (var n = 0; n < rows.length; ++n) {
            var found = -1;
            for (var k = n; k < messageModel.count; ++k) {
                var prior = messageModel.get(k);
                if (prior.recordId === rows[n].recordId ||
                    (prior.isStreaming && rows[n].messageBody.role === "assistant" &&
                     prior.requestId === rows[n].requestId)) { found = k; break; }
            }
            if (found < 0) messageModel.insert(n, rows[n]);
            else {
                if (found !== n) messageModel.move(found, n, 1);
                if (readingAnchor && readingAnchor.id === messageModel.get(n).recordId)
                    readingAnchor.id = rows[n].recordId;
                ["recordId", "requestId", "messageBody", "results", "interrupted", "isStreaming"].forEach(function(key) {
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
        Qt.callLater(restoreAnchor);
        scheduleTailScroll();
    }

    function stopFollowing() {
        followTail = false;
        tailScrollTimer.stop();
    }

    function finishInteraction() {
        followTail = atYEnd;
        captureAnchor();
        scheduleTailScroll();
    }

    function captureAnchor() {
        if (followTail || restoringPosition) return;
        var index = indexAt(1, contentY + spacing + 1);
        var item = itemAtIndex(index);
        if (item) readingAnchor = {id: messageModel.get(index).recordId, offset: item.y - contentY};
    }

    function restoreAnchor() {
        if (followTail || moving || conversationScroll.pressed || !readingAnchor) return;
        restoringPosition = true;
        forceLayout();
        for (var i = 0; i < messageModel.count; ++i) {
            if (messageModel.get(i).recordId !== readingAnchor.id) continue;
            var item = itemAtIndex(i);
            if (!item) {
                positionViewAtIndex(i, ListView.Beginning);
                forceLayout();
                item = itemAtIndex(i);
            }
            if (item) contentY = item.y - readingAnchor.offset;
            break;
        }
        restoringPosition = false;
    }

    function scheduleTailScroll() {
        if (followTail && !moving && !conversationScroll.pressed && !tailScrollTimer.running)
            tailScrollTimer.start();
    }

    function scrollToTail() {
        if (!followTail || moving || conversationScroll.pressed) return;
        forceLayout();
        positionViewAtEnd();
    }

    function jumpToTail() {
        followTail = true;
        scrollToTail();
        scheduleTailScroll();
    }

    Timer {
        id: tailScrollTimer
        interval: 16
        repeat: false
        onTriggered: conversation.scrollToTail()
    }

    onMessagesChanged: updateMessages()
    onInterruptedChanged: syncLiveRow()
    onSessionIdChanged: {
        resetLive();
        messageModel.clear();
        followTail = true;
        readingAnchor = null;
    }
    onContentHeightChanged: {
        scheduleTailScroll();
        if (!followTail) Qt.callLater(restoreAnchor);
    }
    onHeightChanged: {
        scheduleTailScroll();
        if (!followTail) Qt.callLater(restoreAnchor);
    }
    onOriginYChanged: {
        scheduleTailScroll();
        if (!followTail) Qt.callLater(restoreAnchor);
    }
    onMovementStarted: stopFollowing()
    onContentYChanged: {
        if (moving || conversationScroll.pressed || interactionTimer.running)
            captureAnchor();
    }
    onMovementEnded: finishInteraction()

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
        onDisclosureToggling: {
            conversation.stopFollowing();
            conversation.captureAnchor();
        }
        onReasoningToggled: function(expanded) {
            if (recordId !== "__streaming__") return;
            conversation.liveReasoningExpanded = expanded;

        }
    }
}
