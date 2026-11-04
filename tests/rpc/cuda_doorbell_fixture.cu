#include "strata/kernels/elementwise.hpp"
#include <cuda_runtime.h>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
using namespace strata::kernels;
#define CUDA(x) do {auto e=(x);if(e!=cudaSuccess){std::fprintf(stderr,"%s: %s\n",#x,cudaGetErrorString(e));std::exit(2);}}while(0)
struct Shared { alignas(64) uint32_t seq; alignas(64) uint32_t flag; alignas(64) float x[20]; int32_t ids[4]; float w[4]; };
__global__ void ack_wait(const volatile uint32_t* flag,unsigned want){while(*flag<want)__nanosleep(64);}
int main(int argc,char**argv){
 int iterations=argc>1?std::atoi(argv[1]):10000;if(iterations<1)return 2;
 Shared *h,*d;CUDA(cudaHostAlloc(&h,sizeof(Shared),cudaHostAllocMapped));CUDA(cudaHostGetDevicePointer(&d,h,0));
 float *x,*w;int32_t *ids,*res;CUDA(cudaMalloc(&x,16*sizeof(float)));CUDA(cudaMalloc(&w,2*sizeof(float)));CUDA(cudaMalloc(&ids,2*sizeof(int32_t)));CUDA(cudaMalloc(&res,2*sizeof(int32_t)));
 float hx[16],hw[2]={.25f,.75f};for(int i=0;i<16;++i)hx[i]=float(i+1);int32_t hi[2]={0,1};
 CUDA(cudaMemcpy(x,hx,sizeof hx,cudaMemcpyHostToDevice));CUDA(cudaMemcpy(w,hw,sizeof hw,cudaMemcpyHostToDevice));CUDA(cudaMemcpy(ids,hi,sizeof hi,cudaMemcpyHostToDevice));
 cudaStream_t stream;CUDA(cudaStreamCreate(&stream));
 for(bool store:{false,true})for(int type=0;type<3;++type)for(int groups:{1,2}){
  int32_t hr[2]={0,type==2?-1:1};CUDA(cudaMemcpy(res,hr,sizeof hr,cudaMemcpyHostToDevice));
  int steps=48*groups;cudaGraph_t graph;cudaGraphExec_t exec;
  CUDA(cudaStreamBeginCapture(stream,cudaStreamCaptureModeGlobal));
  for(int step=1;step<=steps;++step){
   if(type==0){if(store)doorbell_publish_value(x,ids,w,16,2,d->x,d->ids,d->w,&d->seq,step,stream);else doorbell_publish(x,ids,w,16,2,d->x,d->ids,d->w,&d->seq,stream);}
   else {if(store)doorbell_publish_res_value(x,ids,res,2,16,2,d->x,d->ids,&d->seq,step,stream);else doorbell_publish_res(x,ids,res,2,16,2,d->x,d->ids,&d->seq,stream);}
   ack_wait<<<1,1,0,stream>>>(&d->flag,step);
  }
  CUDA(cudaStreamEndCapture(stream,&graph));CUDA(cudaGraphInstantiate(&exec,graph,0));
  for(int it=0;it<iterations;++it){
   h->seq=0;h->flag=0;for(auto &v:h->x)v=-7;for(auto &v:h->ids)v=-7;for(auto &v:h->w)v=-7;std::atomic_thread_fence(std::memory_order_seq_cst);
   CUDA(cudaGraphLaunch(exec,stream));
   for(int step=1;step<=steps;++step){
    auto start=std::chrono::steady_clock::now();
    while(*(volatile uint32_t*)&h->seq<unsigned(step)){
     if(std::chrono::steady_clock::now()-start>std::chrono::seconds(2)){std::fprintf(stderr,"timeout store=%d type=%d group=%d iteration=%d step=%d seq=%u\n",store,type,groups,it,step,h->seq);std::exit(3);}
    }
    std::atomic_thread_fence(std::memory_order_seq_cst);
    bool ok=h->seq==unsigned(step)&&h->ids[0]==0&&h->ids[1]==1;
    for(int j=0;j<16;++j)ok=ok&&h->x[j]==(type==1?-7:hx[j]);
    for(int j=16;j<20;++j)ok=ok&&h->x[j]==-7;
    ok=ok&&h->ids[2]==-7&&h->ids[3]==-7;
    for(int j=0;j<4;++j)ok=ok&&h->w[j]==(type==0&&j<2?hw[j]:-7);
    if(!ok){std::fprintf(stderr,"payload mismatch store=%d type=%d group=%d iteration=%d step=%d seq=%u\n",store,type,groups,it,step,h->seq);std::exit(4);}
    std::atomic_thread_fence(std::memory_order_seq_cst);*(volatile uint32_t*)&h->flag=step;
   }
   CUDA(cudaStreamSynchronize(stream));
  }
  std::printf("PASS mode=%s path=%d groups=%d windows=%d steps=%d\n",store?"store":"increment",type,groups,iterations,steps);std::fflush(stdout);
  CUDA(cudaGraphExecDestroy(exec));CUDA(cudaGraphDestroy(graph));
 }
 CUDA(cudaStreamDestroy(stream));CUDA(cudaFree(x));CUDA(cudaFree(w));CUDA(cudaFree(ids));CUDA(cudaFree(res));CUDA(cudaFreeHost(h));
}
