import gc
import os
import types

import torch
from torch.nn import functional as F
import torch.nn as nn

MyModule = nn.Module


def __nop(ob):
    return ob


MyFunction = __nop

if os.environ['RWKV_JIT_ON'] == '1':
    MyModule = torch.jit.ScriptModule
    MyFunction = torch.jit.script_method

print(f'\nRWKV_JIT_ON {os.environ["RWKV_JIT_ON"]}\n')

RWKV_RESCALE_LAYER = 6  # set x = x/2 every X layer (to avoid FP16 overflow)


class RWKV_RNN(MyModule):
    def __init__(self, args):
        super().__init__()

        self.args = args
        if args.FLOAT_MODE == 'fp32':
            self.FLOAT_MODE = torch.float
        elif args.FLOAT_MODE == 'fp16':
            self.FLOAT_MODE = torch.half
        elif args.FLOAT_MODE == 'bf16':
            self.FLOAT_MODE = torch.bfloat16
        self.RUN_DEVICE = args.RUN_DEVICE

        with torch.no_grad():
            w = torch.load(args.MODEL_NAME + '.pth', map_location='cpu')
            # refine weights and send to correct device
            keys = list(w.keys())
            print_need_newline = False
            for x in keys:
                block_id = 0
                if 'blocks.' in x:
                    block_id = int(x.split('.')[1])
                if 'att.output.weight' in x:
                    w[x] = w[x] / (2 ** int(block_id // RWKV_RESCALE_LAYER))
                if 'ffn.value.weight' in x:
                    w[x] = w[x] / (2 ** int(block_id // RWKV_RESCALE_LAYER))

                if '.time_' in x:
                    w[x] = w[x].squeeze()
                if '.time_decay' in x:
                    w[x] = w[x].float()
                    w[x] = -torch.exp(w[x])
                elif '.time_first' in x:
                    w[x] = w[x].float()
                else:
                    w[x] = w[x].to(dtype=self.FLOAT_MODE)

                if 'key.weight' in x or 'value.weight' in x or 'receptance.weight' in x or 'output.weight' in x:
                    w[x] = w[x].t()
                w[x].requires_grad = False
                if args.RUN_DEVICE == 'cuda' and x != 'emb.weight':
                    w[x] = w[x].cuda()

                if ('blocks.' not in x) or ('blocks.0.' in x):
                    if print_need_newline:
                        print('\n', end='')
                        print_need_newline = False
                    print(x.ljust(40), str(w[x].dtype).replace('torch.', '').ljust(10), w[x].device)
                else:
                    print_need_newline = True
                    print('.', end='', flush=True)

        # store weights in self.w
        keys = list(w.keys())
        self.w = types.SimpleNamespace()
        for x in keys:
            xx = x.split('.')
            here = self.w
            for i in range(len(xx)):
                if xx[i].isdigit():
                    ii = int(xx[i])
                    if ii not in here:
                        here[ii] = types.SimpleNamespace()
                    here = here[ii]
                else:
                    if i == len(xx) - 1:
                        setattr(here, xx[i], w[x])
                    elif not hasattr(here, xx[i]):
                        if xx[i + 1].isdigit():
                            setattr(here, xx[i], {})
                        else:
                            setattr(here, xx[i], types.SimpleNamespace())
                    here = getattr(here, xx[i])

        self.eval()
        gc.collect()
        torch.cuda.empty_cache()

    def LN(self, x, w):
        return F.layer_norm(x, (self.args.n_embd,), weight=w.weight, bias=w.bias)

    # state[] 0=ffn_xx 1=att_xx 2=att_aa 3=att_bb 4=att_pp

    @MyFunction
    def FF_one(self, x, state, i: int, time_mix_k, time_mix_r, kw, vw, rw):
        xx = state[5 * i + 0].to(dtype=self.FLOAT_MODE)
        xk = x * time_mix_k + xx * (1 - time_mix_k)
        xr = x * time_mix_r + xx * (1 - time_mix_r)
        state[5 * i + 0] = x.float()

        r = torch.sigmoid(xr @ rw)
        k = torch.square(torch.relu(xk @ kw))
        kv = k @ vw
        return r * kv

    @MyFunction
    def FF_seq(self, x, state, i: int, time_mix_k, time_mix_r, kw, vw, rw):
        xx = torch.cat((state[5 * i + 0].to(dtype=self.FLOAT_MODE).unsqueeze(0), x[:-1, :]))
        xk = x * time_mix_k + xx * (1 - time_mix_k)
        xr = x * time_mix_r + xx * (1 - time_mix_r)
        state[5 * i + 0] = x[-1, :].float()

        r = torch.sigmoid(xr @ rw)
        k = torch.square(torch.relu(xk @ kw))
        kv = k @ vw
        return r * kv

    @MyFunction
    def SA_one(self, x, state, i: int, time_mix_k, time_mix_v, time_mix_r, time_first, time_decay, kw, vw, rw, ow):
        xx = state[5 * i + 1].to(dtype=self.FLOAT_MODE)
        xk = x * time_mix_k + xx * (1 - time_mix_k)
        xv = x * time_mix_v + xx * (1 - time_mix_v)
        xr = x * time_mix_r + xx * (1 - time_mix_r)
        state[5 * i + 1] = x.float()

        r = torch.sigmoid(xr @ rw)
        k = (xk @ kw).float()
        v = (xv @ vw).float()

        aa = state[5 * i + 2]
        bb = state[5 * i + 3]
        pp = state[5 * i + 4]
        ww = time_first + k
        p = torch.maximum(pp, ww)
        e1 = torch.exp(pp - p)
        e2 = torch.exp(ww - p)
        a = e1 * aa + e2 * v
        b = e1 * bb + e2
        ww = pp + time_decay
        p = torch.maximum(ww, k)
        e1 = torch.exp(ww - p)
        e2 = torch.exp(k - p)
        state[5 * i + 2] = e1 * aa + e2 * v
        state[5 * i + 3] = e1 * bb + e2
        state[5 * i + 4] = p
        wkv = (a / b).to(dtype=self.FLOAT_MODE)
        return (r * wkv) @ ow

    @MyFunction
    def SA_seq(self, x, state, i: int, time_mix_k, time_mix_v, time_mix_r, time_first, time_decay, kw, vw, rw, ow):
        xx = torch.cat((state[5 * i + 0].to(dtype=self.FLOAT_MODE).unsqueeze(0), x[:-1, :]))
        xk = x * time_mix_k + xx * (1 - time_mix_k)
        xv = x * time_mix_v + xx * (1 - time_mix_v)
        xr = x * time_mix_r + xx * (1 - time_mix_r)
        state[5 * i + 1] = x[-1, :].float()

        r = torch.sigmoid(xr @ rw)
        k = (xk @ kw).float()
        v = (xv @ vw).float()

        aa = state[5 * i + 2]
        bb = state[5 * i + 3]
        pp = state[5 * i + 4]
        T = x.shape[0]
        for t in range(T):
            ww = time_first + k[t]
            p = torch.maximum(pp, ww)
            e1 = torch.exp(pp - p)
            e2 = torch.exp(ww - p)
            a = e1 * aa + e2 * v[t]
            b = e1 * bb + e2
            ww = pp + time_decay
            p = torch.maximum(ww, k[t])
            e1 = torch.exp(ww - p)
            e2 = torch.exp(k[t] - p)
            if t != T - 1:
                aa = e1 * aa + e2 * v[t]
                bb = e1 * bb + e2
                pp = p
            else:
                state[5 * i + 2] = e1 * aa + e2 * v[t]
                state[5 * i + 3] = e1 * bb + e2
                state[5 * i + 4] = p
            xx[t] = (a / b).to(dtype=self.FLOAT_MODE)
        return (r * xx) @ ow

    def forward(self, tokens, state, preprocess_only=False):
        with torch.no_grad():
            w = self.w
            args = self.args

            seq_mode = len(tokens) > 1

            x = w.emb.weight[tokens] if seq_mode else w.emb.weight[tokens[-1]]
            if self.RUN_DEVICE == 'cuda':
                x = x.cuda()

            if state == None:
                state = torch.zeros(args.n_layer * 5, args.n_embd, device=self.RUN_DEVICE)
                for i in range(args.n_layer):
                    state[5 * i + 4] -= 1e30

            SA = self.SA_seq if seq_mode else self.SA_one
            FF = self.FF_seq if seq_mode else self.FF_one

            for i in range(args.n_layer):
                if i == 0:
                    x = self.LN(x, w.blocks[i].ln0)

                ww = w.blocks[i].att
                x = x + SA(self.LN(x, w.blocks[i].ln1), state, i,
                           ww.time_mix_k, ww.time_mix_v, ww.time_mix_r, ww.time_first, ww.time_decay,
                           ww.key.weight, ww.value.weight, ww.receptance.weight, ww.output.weight)

                ww = w.blocks[i].ffn
                x = x + FF(self.LN(x, w.blocks[i].ln2), state, i,
                           ww.time_mix_k, ww.time_mix_r,
                           ww.key.weight, ww.value.weight, ww.receptance.weight)

                if (i + 1) % RWKV_RESCALE_LAYER == 0:
                    x = x / 2

            if preprocess_only:
                return state

            x = self.LN(x[-1, :], w.ln_out) if seq_mode else self.LN(x, w.ln_out)
            x = w.head.weight @ x

            return x.float(), state
