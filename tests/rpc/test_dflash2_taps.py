"""CPU-only tests for the optional DFlash2 RPC payload; no model or GPU needed."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class Dflash2TapTests(unittest.TestCase):
    def test_wire_validation(self):
        source = r'''
#include "strata/rpc/dflash2_taps.hpp"
#include <cassert>
using namespace strata::rpc;
int main() {
    TapBlock a{200, 2, 3, {5, 15}, {0,1,2,3,4,5,6,7,8,9,10,11}};
    Writer w; write_taps(w, a);
    TapBlock b; std::string error;
    auto parse = [&](const std::vector<uint8_t>& data, uint64_t pos, uint64_t rows, uint64_t dim,
                     const std::vector<int>& layers) {
        Reader r(data); error.clear();
        return read_taps(r, b, pos, rows, dim, layers, error) && r.done();
    };
    assert(parse(w.data, 200, 2, 3, {5,15})); assert(b.values == a.values);
    assert(!parse(w.data, 201, 2, 3, {5,15}));  // stale sample position
    assert(!parse(w.data, 200, 1, 3, {5,15}));  // wrong speculative window
    assert(!parse(w.data, 200, 2, 4, {5,15}));
    assert(!parse(w.data, 200, 2, 3, {15,5})); // wrong stage/layer ordering
    auto bad = w.data; bad.pop_back(); assert(!parse(bad, 200, 2, 3, {5,15}));
    bad=w.data; bad[0]=2; assert(!parse(bad, 200, 2, 3, {5,15}));
    bad=w.data; bad.push_back(0); assert(!parse(bad, 200, 2, 3, {5,15}));
    bad=w.data; for (int i=0;i<8;++i) bad[56+i]=255; // forged payload length
    assert(!parse(bad, 200, 2, 3, {5,15}));
    TapBlock empty{4096,0,3,{5,15},{}}; Writer z; write_taps(z,empty);
    assert(parse(z.data,4096,0,3,{5,15})); assert(b.values.empty());
    TapBlock no_layers{4,2,3,{},{}}; Writer n; write_taps(n,no_layers);
    assert(parse(n.data,4,2,3,{}));
    assert(stage_taps({5,15,25,35,45},0,24)==std::vector<int>({5,15}));
    assert(stage_taps({5,15,25,35,45},24,48)==std::vector<int>({25,35,45}));
}
'''
        with tempfile.TemporaryDirectory(prefix="strata-df2-taps-") as tmp:
            cpp = Path(tmp) / "test.cpp"
            exe = Path(tmp) / "test"
            cpp.write_text(source)
            subprocess.run(["g++", "-std=c++20", "-Wall", "-Wextra", "-Werror",
                            "-I" + str(ROOT / "include"), str(cpp),
                            str(ROOT / "src/rpc/transport.cpp"), "-lssl", "-lcrypto", "-pthread",
                            "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
