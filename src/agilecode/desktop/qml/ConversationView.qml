pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls

ListView {
    id: conversation
    objectName: "conversationView"
    property string sessionId: ""
    property var messages: []
    property var submittedMessages: []
    // Presentation-only record of inputs submitted from this application's composer.
    // Model role "user" alone does not identify the person who wrote a message.
    property var locallyAuthoredInputIds: ({})
    property string presentationScope: ""
    property bool ticketConversation: false
    property bool dark: true
    property string assistantName: "Assistant"
    property bool interrupted: false
    property bool reasoningExpandedByDefault: false
    property bool toolsExpandedByDefault: false
    // Apply bulk disclosure changes only after retaining the current reading row.
    property bool appliedToolsExpandedByDefault: false
    // Keep manual toggles through streamed updates and ListView virtualization.
    property var toolExpansionOverrides: ({})
    property bool allowMessageDeletion: false
    property string liveText: ""
    property string liveReasoning: ""
    property var liveToolCalls: []
    property string liveRequestId: ""
    property string liveTurnId: ""
    property string activeRequestId: ""
    property string processingText: ""
    property string processingRequestId: ""
    property string processingTurnId: ""
    property var invalidatedTurns: ({})
    property var invalidatedRequests: ({})
    property var pendingRows: []
    property var savedRequests: ({})
    property bool followTail: true
    property bool liveReasoningExpanded: false
    property var readingAnchor: null
    property bool restoringPosition: false
    // Native ListView tail positioning must not be mistaken for a user gesture.
    property bool positioningTail: false
    property bool anchorNeedsRestore: false
    property string findTerm: ""
    property var findMatches: []
    property int findCursor: -1
    property int highlightedIndex: -1
    property var reasoningExpansionOverrides: ({})
    signal olderHistoryRequested()
    signal messageDeletionRequested(string messageId)
    signal draftRestoreRequested(string submissionId)
    readonly property bool hasLiveMessage: liveText.length > 0 || liveReasoning.length > 0 || liveToolCalls.length > 0
    clip: true
    spacing: 8
    boundsBehavior: Flickable.StopAtBounds
    model: messageModel
    // Darker conversation canvas, independent of the scrolling message content.
    // Keep the light theme and existing message surfaces unchanged.
    Rectangle {
        objectName: "conversationCanvas"
        parent: conversation
        anchors.fill: conversation
        z: -1
        color: conversation.dark ? "#101923" : "transparent"
    }
    ScrollBar.vertical: ScrollBar {
        id: conversationScroll
        objectName: "conversationScrollBar"
        onPressedChanged: {
            if (pressed) conversation.beginInteraction();
            else conversation.finishInteraction();
        }
    }
    // Observe the gesture, but let the native ListView receive the wheel event.
    WheelHandler {
        target: null
        blocking: false
        onWheel: function(event) {
            conversation.beginInteraction();
            interactionTimer.restart();
        }
    }
    Timer {
        id: interactionTimer
        interval: 120
        onTriggered: conversation.finishInteraction()
    }
    ListModel { id: messageModel; dynamicRoles: true }
    // Frame-rate batching: the provider can emit dozens of tokens per second.
    // Update the Qt text document at most once per 100 ms, not once per token.
    Timer {
        id: liveFlushTimer
        interval: 100
        repeat: false
        onTriggered: conversation.syncLiveRow()
    }

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
            isStreaming: !invalidatedRequests[liveRequestId],
            submissionId: "", deliveryStatus: "", canRestore: false,
            renderVersion: "live-" + liveRequestId
        };
        if (index < 0) {
            messageModel.append(row);
        } else {
            messageModel.setProperty(index, "messageBody", row.messageBody);
            messageModel.setProperty(index, "interrupted", row.interrupted);
            messageModel.setProperty(index, "isStreaming", row.isStreaming);
        }
        scheduleTailScroll();
        if (findTerm.trim()) rebuildFind();
    }

    function resetHistory() {
        resetLive(false);
        liveTurnId = "";
        activeRequestId = "";
        clearProcessing();
        invalidatedTurns = ({});
        invalidatedRequests = ({});
        toolExpansionOverrides = ({});
        pendingRows = [];
        savedRequests = ({});
        messageModel.clear();
        readingAnchor = null;
        anchorNeedsRestore = false;
        followTail = true;
        findMatches = [];
        findCursor = -1;
        highlightedIndex = -1;
        reasoningExpansionOverrides = ({});
    }

    function resetLive(preserveIncomplete) {
        liveFlushTimer.stop();
        // Tool-use rounds can start before the saved history has returned.
        // Keep the preceding completed round visible until it is persisted.
        if (preserveIncomplete && liveRequestId && hasLiveMessage && !savedRequests[liveRequestId]) {
            var retained = pendingRows.filter(function(row) { return row.requestId !== liveRequestId; });
            retained.push({
                recordId: "__pending__" + liveRequestId,
                requestId: liveRequestId,
                messageBody: liveBody(),
                results: ({}),
                interrupted: interrupted,
                isStreaming: false,
                submissionId: "", deliveryStatus: "", canRestore: false,
                renderVersion: "pending-" + liveRequestId
            });
            pendingRows = retained;
        }
        var index = liveRowIndex();
        if (index >= 0) {
            if (preserveIncomplete && pendingRows.length &&
                pendingRows[pendingRows.length - 1].requestId === liveRequestId) {
                var parked = pendingRows[pendingRows.length - 1];
                ["recordId", "messageBody", "interrupted", "isStreaming", "renderVersion"].forEach(function(key) {
                    messageModel.setProperty(index, key, parked[key]);
                });
            } else {
                messageModel.remove(index);
            }
        }
        liveText = "";
        liveReasoning = "";
        liveToolCalls = [];
        liveRequestId = "";
        liveReasoningExpanded = reasoningExpandedByDefault;
    }

    function invalidateTurn(turnId) {
        var turns = Object.assign({}, invalidatedTurns);
        if (turnId) turns[turnId] = true;
        var affectsLive = !turnId || !liveTurnId || liveTurnId === turnId;
        var affectsProcessing = !turnId || !processingTurnId || processingTurnId === turnId;
        if (affectsLive && liveTurnId) turns[liveTurnId] = true;
        invalidatedTurns = turns;
        var requests = Object.assign({}, invalidatedRequests);
        if (affectsLive && liveRequestId) requests[liveRequestId] = true;
        if (affectsLive && activeRequestId) requests[activeRequestId] = true;
        if (affectsProcessing && processingRequestId) requests[processingRequestId] = true;
        invalidatedRequests = requests;
        if (affectsLive) {
            // Keep the already received prefix readable, freeze it immediately,
            // and reject every later packet for this invalidated generation.
            liveFlushTimer.stop();
            interrupted = true;
            syncLiveRow();
        }
        if (affectsProcessing) clearProcessing();
    }

    function clearProcessing() {
        processingText = "";
        processingRequestId = "";
        processingTurnId = "";
    }

    function generationStarted(data) {
        if (invalidatedTurns[data.turn_id] || invalidatedRequests[data.request_id]) return;
        if (data.purpose && data.purpose !== "conversation") {
            processingText = data.purpose === "context_preparation" ? "Compressing context…" : "Preparing response…";
            processingRequestId = data.request_id || "";
            processingTurnId = data.turn_id || "";
            scheduleTailScroll();
        } else {
            clearProcessing();
            activeRequestId = data.request_id || "";
            if (liveRequestId && invalidatedRequests[liveRequestId] && liveRequestId !== data.request_id) resetLive(true);
            liveTurnId = data.turn_id || "";
            interrupted = false;
        }
    }

    function generationFinished(data) {
        if (processingRequestId && processingRequestId === data.request_id) clearProcessing();
        if ((!data.purpose || data.purpose === "conversation") &&
                (!liveRequestId || liveRequestId === data.request_id)) interrupted = !!data.stopped ||
                    !!invalidatedRequests[data.request_id] || !!invalidatedTurns[data.turn_id];
    }

    function appendDelta(event) {
        if (!sessionId || event.session_id !== sessionId) return;
        var requestId = event.data.request_id;
        if (invalidatedRequests[requestId] || invalidatedTurns[event.data.turn_id]) return;
        // Internal summaries/drafts are operational work, never assistant chat.
        if (event.data.purpose && event.data.purpose !== "conversation") {
            generationStarted(event.data);
            return;
        }
        liveTurnId = event.data.turn_id || liveTurnId;
        // Ignore late packets for a turn already present in saved history.
        if (savedRequests[requestId]) return;
        if (liveRequestId !== requestId) {
            resetLive(true);
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
        if (liveRowIndex() < 0) syncLiveRow();
        else if (!liveFlushTimer.running) liveFlushTimer.start();
    }

    function isHumanAuthored(body, inputId) {
        if (!body || body.role !== "user") return false;
        if (inputId && locallyAuthoredInputIds[JSON.stringify([presentationScope, sessionId, inputId])])
            return true;
        // Older ticket feedback carries an explicit, engine-defined human
        // feedback marker; generic user-role instructions and PM updates do not.
        return ticketConversation && typeof body.content === "string" &&
               body.content.indexOf("USER FEEDBACK: ") === 0;
    }

    function updateMessages() {
        captureAnchor();
        var rows = [];
        var queuedInputs = [];
        var consumed = ({});
        var saved = ({});
        for (var i = 0; i < messages.length; ++i) {
            var record = messages[i];
            if (consumed[i]) continue;
            var results = ({});
            if (record.body.role === "assistant") {
                if (record.request_id) saved[record.request_id] = true;
                var calls = record.body.tool_calls || [];
                for (var j = i + 1; j < messages.length && messages[j].body.role === "tool"; ++j) {
                    var result = messages[j].body;
                    if (calls.some(function(call) { return call.id === result.tool_call_id; })) {
                        results[result.tool_call_id] = result;
                        consumed[j] = true;
                    }
                }
            }
            var row = {
                recordId: record.id || String(record.seq),
                requestId: record.request_id || "",
                messageBody: record.body,
                results: results,
                interrupted: record.body.role === "assistant" && !!record.partial,
                isStreaming: false,
                submissionId: record.body.role === "user" ? (record.request_id || "") : "",
                deliveryStatus: record.body.role === "user" && record.partial ? "queued" : "",
                canRestore: false,
                renderVersion: (record.id || String(record.seq)) + "|" + (!!record.partial) + "|" +
                    Object.keys(results).map(function(id) { return id + ":" + (results[id].id || ""); }).join(",")
            };
            // Accepted input is durable before old work finishes. Keep queued
            // users after that old live/pending prefix until the engine includes
            // them and establishes their final canonical transcript order.
            if (record.body.role === "user" && record.partial) queuedInputs.push(row);
            else rows.push(row);
        }

        pendingRows = pendingRows.filter(function(row) { return !saved[row.requestId]; });
        for (var p = 0; p < pendingRows.length; ++p) rows.push(pendingRows[p]);
        var savedLive = !!(liveRequestId && saved[liveRequestId]);
        if (hasLiveMessage && !savedLive) {
            rows.push({recordId: "__streaming__", requestId: liveRequestId,
                       messageBody: liveBody(), results: ({}), interrupted: interrupted,
                       isStreaming: !invalidatedRequests[liveRequestId], submissionId: "", deliveryStatus: "", canRestore: false,
                       renderVersion: "live-" + liveRequestId});
        }
        rows = rows.concat(queuedInputs);
        // Submitted user input remains visible while the old exchange settles.
        // Match only durable identities, never content (identical messages are
        // distinct inputs), and promote the same delegate when history arrives.
        for (var u = 0; u < submittedMessages.length; ++u) {
            var submission = submittedMessages[u];
            var savedInput = rows.find(function(row) { return row.messageBody.role === "user" &&
                ((submission.messageId && row.recordId === submission.messageId) ||
                 row.submissionId === submission.requestId); });
            if (savedInput) {
                savedInput.submissionId = submission.requestId;
                continue;
            }
            rows.push({recordId: "__submission__" + submission.requestId,
                requestId: "", submissionId: submission.requestId,
                messageBody: {role: "user", content: submission.text}, results: ({}),
                interrupted: false, isStreaming: false, deliveryStatus: submission.status,
                canRestore: ["failed", "unknown"].indexOf(submission.status) >= 0 && !submission.accepted,
                renderVersion: "submission-" + submission.requestId + "|" + submission.status});
        }
        // Insert older history without replacing existing delegates. Promote the
        // live row in place when its persisted assistant message arrives.
        for (var n = 0; n < rows.length; ++n) {
            var found = -1;
            for (var k = n; k < messageModel.count; ++k) {
                var prior = messageModel.get(k);
                if (prior.recordId === rows[n].recordId ||
                    (prior.submissionId && prior.submissionId === rows[n].submissionId &&
                     rows[n].messageBody.role === "user") ||
                    ((prior.isStreaming || prior.recordId.indexOf("__pending__") === 0) &&
                     rows[n].messageBody.role === "assistant" &&
                     prior.requestId === rows[n].requestId)) { found = k; break; }
            }
            if (found < 0) messageModel.insert(n, rows[n]);
            else {
                if (found !== n) messageModel.move(found, n, 1);
                if (readingAnchor && readingAnchor.id === messageModel.get(n).recordId)
                    readingAnchor.id = rows[n].recordId;
                // Saved records never change in place. Their IDs and result
                // message IDs suffice to detect updates; never stringify an
                // entire reasoning transcript or tool result for comparison.
                if (messageModel.get(n).renderVersion !== rows[n].renderVersion ||
                    messageModel.get(n).recordId !== rows[n].recordId ||
                    messageModel.get(n).isStreaming !== rows[n].isStreaming) {
                    ["recordId", "requestId", "messageBody", "results", "interrupted", "isStreaming", "submissionId", "deliveryStatus", "canRestore", "renderVersion"].forEach(function(key) {
                        messageModel.setProperty(n, key, rows[n][key]);
                    });
                }
            }
        }
        if (messageModel.count > rows.length)
            messageModel.remove(rows.length, messageModel.count - rows.length);

        savedRequests = saved;
        if (savedLive) {
            liveFlushTimer.stop();
            liveText = "";
            liveReasoning = "";
            liveToolCalls = [];
            liveRequestId = "";
        }
        syncLiveRow();
        if (!followTail && readingAnchor) {
            anchorNeedsRestore = true;
            scheduleAnchorRestore();
        }
        scheduleTailScroll();
        if (findTerm.trim()) rebuildFind();
    }

    function applyToolExpansionDefault() {
        captureAnchor(true);
        appliedToolsExpandedByDefault = toolsExpandedByDefault;
        toolExpansionOverrides = ({});
        if (!followTail && readingAnchor) {
            anchorNeedsRestore = true;
            scheduleAnchorRestore();
        }
        scheduleTailScroll();
    }

    function rememberToolExpansion(messageId, callId, fallbackId, expanded) {
        var overrides = Object.assign({}, toolExpansionOverrides);
        var messageOverrides = Object.assign({}, overrides[messageId] || {});
        messageOverrides[callId] = expanded;
        if (fallbackId) messageOverrides[fallbackId] = expanded;
        overrides[messageId] = messageOverrides;
        toolExpansionOverrides = overrides;
    }

    function rememberReasoningExpansion(id, expanded) {
        var updates = Object.assign({}, reasoningExpansionOverrides);
        updates[id] = expanded;
        reasoningExpansionOverrides = updates;
    }

    function reasoningDefaultFor(id) {
        return Object.prototype.hasOwnProperty.call(reasoningExpansionOverrides, id)
               ? !!reasoningExpansionOverrides[id] : reasoningExpandedByDefault;
    }

    // Search only what this ListView has already received: no new history or
    // backend calls. Tool arguments/results are included even when collapsed.
    function rebuildFind() {
        var term = findTerm.trim().toLowerCase();
        if (!term) {
            findMatches = [];
            findCursor = -1;
            highlightedIndex = -1;
            return;
        }
        var previousId = highlightedIndex >= 0 && highlightedIndex < messageModel.count
                         ? messageModel.get(highlightedIndex).recordId : "";
        var matches = [];
        for (var i = 0; i < messageModel.count; ++i) {
            var row = messageModel.get(i);
            var body = row.messageBody || ({});
            var text = [body.content || "", body.reasoning_content || body.reasoning || "",
                        JSON.stringify(body.tool_calls || []), JSON.stringify(row.results || {})].join("\n");
            if (text.toLowerCase().indexOf(term) >= 0) matches.push(i);
        }
        findMatches = matches;
        var selected = matches.findIndex(function(i) { return messageModel.get(i).recordId === previousId; });
        findCursor = selected;
        if (selected < 0) highlightedIndex = -1;
    }

    function viewportIndex() {
        var i = indexAt(1, contentY + spacing + 1);
        if (i < 0) i = indexAt(1, contentY + height / 2);
        return i < 0 ? Math.max(0, messageModel.count - 1) : i;
    }

    function focusMessage(index) {
        if (index < 0 || index >= messageModel.count) return;
        stopFollowing();
        highlightedIndex = index;
        positionViewAtIndex(index, ListView.Beginning);
        forceLayout();
        captureAnchor();
    }

    function navigateMessage(direction) {
        if (!messageModel.count) return;
        var start = highlightedIndex >= 0 ? highlightedIndex : viewportIndex();
        for (var i = start + direction; i >= 0 && i < messageModel.count; i += direction) {
            if ((messageModel.get(i).messageBody || {}).role !== "tool") {
                focusMessage(i);
                return;
            }
        }
    }

    function navigateFind(direction) {
        if (!findMatches.length) return;
        var start = findCursor;
        if (start < 0) start = direction > 0 ? -1 : 0;
        findCursor = (start + direction + findMatches.length) % findMatches.length;
        focusMessage(findMatches[findCursor]);
    }

    function isAtTail(tolerance) {
        // Prefer the view's actual end state; when the size estimate is
        // unsettled, compare the visible last item to the viewport instead.
        if (atYEnd) return true;
        var last = itemAtIndex(messageModel.count - 1);
        var endItem = footerItem && footerItem.visible && footerItem.height > 0
                      ? footerItem : last;
        return !!endItem && Math.abs(endItem.y + endItem.height -
                                    (contentY + height)) <= (tolerance === undefined ? 2 : tolerance);
    }

    function stopFollowing() {
        followTail = false;
        tailScrollTimer.stop();
    }

    function beginInteraction() {
        stopFollowing();
        captureAnchor();
    }

    function finishInteraction() {
        // Resume following only after the user has actually returned to the
        // bottom. Their former position is not evidence they want to follow.
        if (isAtTail(2)) {
            followTail = true;
            readingAnchor = null;
            anchorNeedsRestore = false;
            scheduleTailScroll();
        } else {
            stopFollowing();
            captureAnchor();
        }
        requestOlderHistoryIfNeeded();
    }

    function requestOlderHistoryIfNeeded() {
        if (!followTail && height > 0 && contentHeight > height
                && contentY <= originY + 48)
            olderHistoryRequested();
    }

    function captureAnchor(includeTool) {
        if (followTail || restoringPosition) return;
        var index = indexAt(1, contentY + spacing + 1);
        if (includeTool && index < 0) index = indexAt(1, contentY);
        var item = itemAtIndex(index);
        if (item) {
            readingAnchor = {id: messageModel.get(index).recordId, offset: item.y - contentY};
            if (includeTool)
                readingAnchor.tool = item.captureToolAnchor(contentY - item.y, height, toolsExpandedByDefault);
        }
    }

    function scheduleAnchorRestore() {
        if (!followTail && readingAnchor && anchorNeedsRestore && !anchorRestoreTimer.running)
            anchorRestoreTimer.start();
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
            if (item) {
                var toolPosition = readingAnchor.tool ? item.toolAnchorPosition(readingAnchor.tool.key) : null;
                contentY = toolPosition === null ? item.y - readingAnchor.offset
                    : item.y + toolPosition - readingAnchor.tool.offset;
            }
            break;
        }
        anchorNeedsRestore = false;
        restoringPosition = false;
    }

    function scheduleTailScroll() {
        // Coalesce layout and streaming changes into one correction per frame
        // instead of a zero-delay feedback loop.
        if (followTail && !positioningTail && !moving &&
                !conversationScroll.pressed && !tailScrollTimer.running && height > 0)
            tailScrollTimer.start();
    }

    function scrollToTail(force) {
        if (!followTail || moving || conversationScroll.pressed || height <= 0) return;
        if (!force) {
            // Trust the actual last item when available: atYEnd alone can be
            // misleading while Qt is estimating variable delegate heights.
            var last = itemAtIndex(messageModel.count - 1);
            var endItem = footerItem && footerItem.visible && footerItem.height > 0
                          ? footerItem : last;
            if (endItem) {
                if (endItem.y + endItem.height <= contentY + height + 1) return;
            } else if (atYEnd) {
                return;
            }
        }
        positioningTail = true;
        try {
            // ListView resolves variable-height delegates and footer geometry.
            // Writing an estimated contentY here can oscillate violently.
            positionViewAtEnd();
        } finally {
            positioningTail = false;
        }
    }

    function jumpToTail() {
        cancelFlick();
        interactionTimer.stop();
        anchorRestoreTimer.stop();
        readingAnchor = null;
        anchorNeedsRestore = false;
        followTail = true;
        tailScrollTimer.stop();
        scrollToTail(true);
        scheduleTailScroll();
    }

    Timer {
        id: tailScrollTimer
        interval: 32
        repeat: false
        onTriggered: conversation.scrollToTail(false)
    }
    Timer {
        id: anchorRestoreTimer
        interval: 0
        repeat: false
        onTriggered: conversation.restoreAnchor()
    }

    onMessagesChanged: updateMessages()
    onFindTermChanged: rebuildFind()
    onToolsExpandedByDefaultChanged: applyToolExpansionDefault()
    onReasoningExpandedByDefaultChanged: reasoningExpansionOverrides = ({})
    onSubmittedMessagesChanged: updateMessages()
    onInterruptedChanged: syncLiveRow()
    onSessionIdChanged: {
        resetLive(false);
        liveTurnId = "";
        activeRequestId = "";
        clearProcessing();
        invalidatedTurns = ({});
        invalidatedRequests = ({});
        toolExpansionOverrides = ({});
        messageModel.clear();
        pendingRows = [];
        savedRequests = ({});
        followTail = true;
        readingAnchor = null;
        anchorNeedsRestore = false;
        findMatches = [];
        findCursor = -1;
        highlightedIndex = -1;
        reasoningExpansionOverrides = ({});
        tailScrollTimer.stop();
        anchorRestoreTimer.stop();
    }
    onContentHeightChanged: {
        if (followTail) {
            if (!positioningTail) scheduleTailScroll();
        } else {
            // Nested tool layouts can settle after the first restore timer fires.
            if (readingAnchor && readingAnchor.tool) anchorNeedsRestore = true;
            scheduleAnchorRestore();
        }
    }
    onHeightChanged: {
        if (followTail) {
            if (!positioningTail) scheduleTailScroll();
        } else {
            anchorNeedsRestore = true;
            scheduleAnchorRestore();
        }
    }
    onOriginYChanged: {
        if (followTail) {
            if (!positioningTail) scheduleTailScroll();
        } else {
            anchorNeedsRestore = true;
            scheduleAnchorRestore();
        }
    }
    onMovementStarted: {
        if (!positioningTail) beginInteraction();
    }
    onContentYChanged: {
        if (restoringPosition || positioningTail) return;
        if (moving || conversationScroll.pressed || interactionTimer.running) {
            if (followTail) stopFollowing();
            captureAnchor();
        }
        requestOlderHistoryIfNeeded();
    }
    onMovementEnded: finishInteraction()

    footer: Label {
        objectName: "conversationProcessingStatus"
        width: conversation.width
        height: conversation.processingText ? implicitHeight : 0
        visible: conversation.processingText.length > 0
        text: conversation.processingText
        color: conversation.dark ? "#94aabe" : "#607789"
        padding: 10
        wrapMode: Text.WordWrap
        Accessible.name: text
    }

    delegate: ChatMessage {
        required property int index
        required property string recordId
        required property string requestId
        required property var messageBody
        required property var results
        required property bool interrupted
        required property bool isStreaming
        required property string submissionId
        required property string deliveryStatus
        required property bool canRestore
        objectName: recordId === "__streaming__" ? "streamingMessage" : "chatMessage-" + recordId
        width: conversation.width
        body: messageBody
        locallyAuthored: conversation.isHumanAuthored(messageBody, submissionId || requestId)
        toolResults: results
        partial: interrupted
        streaming: isStreaming
        submissionStatus: deliveryStatus
        draftRecoverable: canRestore
        onRestoreDraftRequested: conversation.draftRestoreRequested(submissionId)
        deletable: conversation.allowMessageDeletion && !isStreaming && recordId !== "__streaming__" &&
                   recordId.indexOf("__pending__") !== 0 && recordId.indexOf("__submission__") !== 0 && messageBody.role !== "system"
        dark: conversation.dark
        reasoningExpandedByDefault: conversation.reasoningDefaultFor(requestId || recordId)
        navigationHighlighted: index === conversation.highlightedIndex
        findTerm: conversation.findTerm
        toolsExpandedByDefault: conversation.appliedToolsExpandedByDefault
        toolExpansionOverrides: conversation.toolExpansionOverrides[requestId || recordId] || ({})
        onToolToggled: function(callId, fallbackId, expanded) { conversation.rememberToolExpansion(requestId || recordId, callId, fallbackId, expanded); }
        assistantName: conversation.assistantName
        onDisclosureToggling: {
            conversation.beginInteraction();
            conversation.captureAnchor();
            conversation.anchorNeedsRestore = true;
            conversation.scheduleAnchorRestore();
        }
        onDeleteRequested: conversation.messageDeletionRequested(recordId)
        onReasoningToggled: function(expanded) {
            conversation.rememberReasoningExpansion(requestId || recordId, expanded);
            if (recordId === "__streaming__") conversation.liveReasoningExpanded = expanded;
        }
    }
}
