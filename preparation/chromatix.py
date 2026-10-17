# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
import struct, sys, collections
class Chromatix:
    def __init__(self, path):
        self.f = open(path,'rb').read()
        w = struct.unpack_from('<13I', self.f, 0x98)
        self.sec = {w[4]:(w[5],w[6]), w[7]:(w[8],w[9]), w[10]:(w[11],w[12])}
        self.data_off = self.sec[1][0]
        self.params = []   # (name, off, size, id)
        off = 0xd0; end = self.sec[0][0] + self.sec[0][1]
        while off + 0x38 <= end:
            a,b,c,d = struct.unpack_from('<4I', self.f, off+40)
            if a != 0xffffffff: break
            name = self.f[off:off+40].split(b'\0')[0].decode()
            self.params.append((name,b,c,d)); off += 0x38
        self.mods = []     # (name, ver, off, size, id)
        while off + 0x40 <= end:
            name = self.f[off:off+40].split(b'\0')[0].decode()
            if not name: break
            maj,mi,f1,f2,o,s,i = struct.unpack_from('<HHIIIII', self.f, off+40)
            self.mods.append((name,(maj,mi),f1,f2,o,s,i)); off += 0x40
        self.byid = {p[3]:p for p in self.params}
    def data(self, off, size):
        return self.f[self.data_off+off:self.data_off+off+size]
    def modules(self):
        """split the param list into modules at each 'revision'"""
        out=[]; cur=None
        for p in self.params:
            if p[0]=='revision':
                cur=[]; out.append(cur)
            cur.append(p)
        return out
if __name__=='__main__':
    c=Chromatix(sys.argv[1])
    print("params",len(c.params),"module descriptors",len(c.mods))
    for m in c.modules():
        names=[p[0] for p in m if p[0].startswith('mod_') or p[0] not in ('revision','control_var_type','trigger','region')]
        regions=[p for p in m if p[0]=='region' and p[2]]
        sizes=collections.Counter(p[2] for p in regions)
        print(f"{(names[0] if names else '?'):40s} entries={len(m):4d} regions={len(regions):3d} sizes={dict(sizes)}")
