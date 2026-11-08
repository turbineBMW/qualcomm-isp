// SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
// SPDX-License-Identifier: MIT
#include "camss_lsc_stats.h"
#include <cassert>
#include <iostream>
using namespace libcamera;
int main()
{
 CamssLscStats model;
 std::vector<uint16_t> mesh(884, 0);
 const std::array<unsigned int,4> q{0,64,400,1023};
 assert(model.configure(mesh) && !model.enabled());
 assert(model.apply(q,0,0)==q);
 std::fill(mesh.begin(),mesh.end(),1024);
 assert(model.configure(mesh) && model.enabled());
 for(unsigned y=0;y<2160;y+=16) for(unsigned x=0;x<3840;x+=16)
  assert(model.apply(q,x,y)==q);
 std::fill(mesh.begin(),mesh.end(),1280);
 assert(model.configure(mesh));
 const std::array<unsigned int,4> boosted{0,64,484,1023};
 assert(model.apply(q,0,0)==boosted && model.apply(q,3824,2144)==boosted);
 // A linear horizontal ramp interpolates to the independently calculated
 // native sample position, including RAW/output scaling and Bayer alignment.
 for(unsigned c=0;c<4;c++) for(unsigned y=0;y<13;y++) for(unsigned x=0;x<17;x++)
  mesh[c*221+y*17+x]=1024+16*x;
 assert(model.configure(mesh));
 for(unsigned x: {0u,480u,1920u,3824u}) {
  auto result=model.apply({64,320,576,832},x,1088);
  unsigned rx=(x*4224/3840)&~1u;
  unsigned gain=std::lround(1024+256.0*(rx/2.0)/2111);
  assert(result[0]==64 && result[2]==64+(512*gain+512)/1024);
 }
 for(auto bad: {1023,1281}) {
  std::fill(mesh.begin(),mesh.end(),bad);assert(!model.configure(mesh));assert(!model.enabled());
 }
 std::fill(mesh.begin(),mesh.end(),1100);mesh[221]=1101;
 assert(!model.configure(mesh)); // per-channel meshes are deliberately unsupported
 mesh.resize(883);assert(!model.configure(mesh));
 std::fill(mesh.begin(),mesh.end(),0);assert(!model.configure(mesh));
 std::cout << "PASS: disabled, unity at every sampled coordinate, pedestal, saturation, native geometry, limits and channel identity\n";
}
