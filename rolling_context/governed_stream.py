"""One corrective attempt, before any productive output is committed downstream."""
import json
import time

from .governor import GenerationGovernor, retry_body, safe_metadata, DIAGNOSTIC


def completion(handler, watch, body, policy, fields, prompt_cap):
    from .relay import RequestError, emit, MAX_SSE_EVENT_BYTES, MAX_JSON_RESPONSE_BYTES
    streaming = bool(body.get('stream'))
    request_id = fields['request_id']
    committed = False
    started = time.monotonic()

    def event(name, governor, reason, attempt):
        emit(handler.server.state_dir, handler.server.profile, name,
             request_id=request_id, session_id=fields.get('session_id'), attempt=attempt,
             reason_code=reason, **governor.metrics())

    def send(delta, finish=None):
        if not getattr(handler, '_governor_headers', False):
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream')
            handler.send_header('Connection', 'close')
            handler.send_header('Cache-Control', 'no-cache')
            handler.send_header('X-Rolling-Context-Request-Id', request_id)
            handler.end_headers()
            handler._governor_headers = True
        payload = {'id':request_id, 'object':'chat.completion.chunk', 'created':int(time.time()),
                   'model':body.get('model','local'),
                   'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
        handler.wfile.write(('data: '+json.dumps(payload)+'\n\n').encode())
        handler.wfile.flush()

    def output(message, finish, usage):
        if streaming:
            send({}, finish)
            if usage:
                handler.wfile.write(('data: '+json.dumps({'id':request_id,'choices':[], 'usage':usage})+'\n\n').encode())
            handler.wfile.write(b'data: [DONE]\n\n')
            handler.wfile.flush()
        else:
            handler._json(200, {'id':request_id, 'object':'chat.completion', 'created':int(time.time()),
                               'model':body.get('model','local'),
                               'choices':[{'index':0,'message':message,'finish_reason':finish}], 'usage':usage})
            handler._governor_headers = True

    current = dict(body)
    for attempt in range(policy['max_retries'] + 1):
        watch.remaining()  # One original deadline, never a new retry deadline.
        if attempt:
            current = retry_body(body, policy)
            retry_tokens = handler._count(watch, current)
            fields['governor_retry_input_tokens'] = retry_tokens
            if retry_tokens > prompt_cap:
                event('generation_governor_stopped', governor, 'retry_prompt_limit', attempt)
                message = {'role':'assistant','content':DIAGNOSTIC}
                if streaming: send(message)
                output(message, 'stop', {})
                return
        current['stream'] = True  # Also govern Hermes' non-streaming default.
        current['stream_options'] = {**current.get('stream_options', {}), 'include_usage':True}
        current['timings_per_token'] = True
        governor = GenerationGovernor(policy)
        event('generation_governor_evaluated', governor, 'started', attempt)
        message = {'role':'assistant','content':''}
        calls = {}
        pending = []
        pending_bytes = 0
        visible_bytes = 0
        timings = {}; usage = {}; reason = None; done = False
        connection, response = handler._open(watch, 'POST', handler.path, current)
        try:
            if response.status != 200:
                raise RequestError(502, 'Governed model request failed.', 'governor_upstream_error')
            if 'text/event-stream' not in response.getheader('Content-Type',''):
                raise RequestError(502, 'Governed model endpoint must support streaming.', 'governor_stream_required')
            data_lines = []; size = 0
            while True:
                line = response.readline(MAX_SSE_EVENT_BYTES + 1)
                watch.remaining()
                if not line:
                    break
                size += len(line)
                if size > MAX_SSE_EVENT_BYTES:
                    raise RequestError(502, 'Model stream event exceeds limit.', 'governor_event_limit')
                if line.startswith(b'data:'):
                    data_lines.append(line[5:].strip())
                if line.strip():
                    continue
                raw = b'\n'.join(data_lines); data_lines.clear(); size = 0
                if raw == b'[DONE]':
                    done = True
                    break
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    if not isinstance(obj, dict): raise ValueError()
                except ValueError:
                    raise RequestError(502, 'Invalid model stream event.', 'governor_invalid_stream') from None
                governor.consume(obj)
                t, u = safe_metadata(obj)
                timings.update(t); usage.update(u)
                # Copy only permitted visible/action fields. Private fields die with this event.
                for choice in obj.get('choices', []):
                    delta = choice.get('delta') or {}
                    clean = {}
                    content = delta.get('content')
                    if isinstance(content, str) and content:
                        clean['content'] = content
                        if not streaming: message['content'] += content
                    for call in delta.get('tool_calls') or []:
                        index = call.get('index', 0)
                        if type(index) is not int or not 0 <= index < 128:
                            raise RequestError(502, 'Invalid tool stream index.', 'governor_invalid_stream')
                        fn = call.get('function') or {}
                        safe = {k:call[k] for k in ('index','id','type') if k in call}
                        safe['function'] = {k:fn[k] for k in ('name','arguments') if isinstance(fn.get(k), str)}
                        clean.setdefault('tool_calls', []).append(safe)
                        if not streaming:
                            target = calls.setdefault(index, {'id':'','type':'function','function':{'name':'','arguments':''}})
                            if 'id' in safe: target['id'] = safe['id']
                            for k,v in safe['function'].items(): target['function'][k] += v
                    # Legacy function-call clients retain their structural action.
                    if isinstance(delta.get('function_call'), dict):
                        clean['function_call'] = {k:v for k,v in delta['function_call'].items()
                                                  if k in {'name','arguments'} and isinstance(v,str)}
                        if not streaming:
                            target=message.setdefault('function_call', {'name':'','arguments':''})
                            for k,v in clean['function_call'].items(): target[k] += v
                    if clean:
                        clean_size = len(json.dumps(clean).encode())
                        visible_bytes += clean_size
                        if visible_bytes > MAX_JSON_RESPONSE_BYTES:
                            raise RequestError(502, 'Visible completion exceeds limit.', 'governor_output_limit')
                        if streaming:
                            if committed:
                                send(clean)
                            else:
                                pending.append(clean); pending_bytes += clean_size
                                if pending_bytes > MAX_SSE_EVENT_BYTES:
                                    raise RequestError(502, 'Pending visible stream exceeds limit.', 'governor_output_limit')
                del obj, raw
                if governor.useful:
                    committed = True
                    if streaming:
                        for delta in pending: send(delta)
                        pending.clear(); pending_bytes = 0
                if governor.soft():
                    event('generation_governor_evaluated', governor, 'soft_no_action_tokens', attempt)
                reason = None if committed else governor.verdict()
                if reason:
                    watch.close_upstreams()  # Same owned-socket cancellation as deadline/disconnect.
                    fields['governor_cancelled'] = True
                    event('generation_governor_triggered', governor, reason, attempt)
                    break
            if not reason and (not done or not governor.finish_reason):
                raise RequestError(502, 'Incomplete model stream.', 'governor_incomplete_stream')
        finally:
            response.close(); connection.close()
        fields.update(inference_ms=round((time.monotonic()-started)*1000),
                      governor_observation=governor.metrics(), timings=timings, usage=usage,
                      stream_done=done, upstream_status=200)
        if not reason and not governor.nonwhite_characters and not governor.tool_call_started:
            reason = 'empty_completion'
            event('generation_governor_triggered', governor, reason, attempt)
        if reason:
            if attempt < policy['max_retries']:
                event('generation_governor_retry', governor, reason, attempt+1)
                continue
            event('generation_governor_stopped', governor, reason, attempt)
            message = {'role':'assistant','content':DIAGNOSTIC}
            if streaming: send(message)
            output(message, 'stop', usage)  # Nonempty stop bypasses Hermes length/empty continuations.
            fields['stream_done'] = True
            return
        if calls: message['tool_calls'] = [calls[i] for i in sorted(calls)]
        if streaming:
            for delta in pending: send(delta)
        event('generation_governor_evaluated', governor, 'productive_completion', attempt)
        output(message, governor.finish_reason, usage)
        return
