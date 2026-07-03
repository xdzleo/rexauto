// XCTD/LZXT offline decompressor (XCOMPRESS_FILE_IDENTIFIER_LZXTDECODE 0x0FF512ED).
// Format cracked empirically on Captain America: Super Soldier and confirmed by
// public prior art (QuickBMS unxmemlzx, UniPyX decompress_xb).
//
// Header (16 bytes, all BE):
//   off0  u32 magic 0x0FF512ED;  off4 u16 version 0x0100;  off6 u16 reserved
//   off8  u32 crc/hash (not needed for decode)
//   off12 u32 flags:
//         bits0-3   window   = 1 << (n+15)
//         bits4-5   zbs      = 0x8000 << n      (zero-pad alignment boundary)
//         bits6-21  segments (u16 count; 0 = payload is RAW at off16)
//         bits22-23 table entry width: 0 = 20-bit MSB-first packed, 1 = BE32
// off16: table of `segments` uncompressed sizes (packed to 32-bit words),
//        then the chunk stream at bdo = 16 + 4*((bps*segments+31)>>5).
//
// Chunk stream: [BE16 size][size bytes]... ; a BE16 of 0 means ZERO PADDING of
// arbitrary BYTE length (odd allowed!) up to the next zbs file-offset boundary.
// 1 chunk = 1 LZX frame (32KB uncompressed, the last frame of a segment is
// partial). Segment k owns the next ceil(size_k/32768) chunks and is an
// independent LZX stream (window from flags). Decompressed file = segments
// concatenated in table order.
//
// usage: xctd_rip <in> <out>   (exit 0 only on fully verified decode)
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <vector>
extern "C" {
#include <system.h>
#include <lzx.h>
extern int lzxd_xmem_no_uncompressed_realign;  // our lzxd.c dialect switch
}

struct memfile { off_t size; off_t off; uint8_t* buf; };
static int  m_read (mspack_file* f, void* b, int n){ memfile* m=(memfile*)f; off_t rem=m->size-m->off; off_t t=(n<rem)?n:rem; if(t<0)t=0; memcpy(b, m->buf+m->off, (size_t)t); m->off+=t; return (int)t; }
static int  m_write(mspack_file* f, void* b, int n){ memfile* m=(memfile*)f; off_t rem=m->size-m->off; off_t t=(n<rem)?n:rem; if(t<0)t=0; memcpy(m->buf+m->off, b, (size_t)t); m->off+=t; return (int)t; }
static void*m_alloc(mspack_system* s, size_t n){ (void)s; return calloc(n,1); }
static void m_free (void* p){ free(p); }
static void m_copy (void* s, void* d, size_t n){ memcpy(d,s,n); }

static uint32_t rd32(const uint8_t* b){ return ((uint32_t)b[0]<<24)|((uint32_t)b[1]<<16)|((uint32_t)b[2]<<8)|b[3]; }
static uint32_t rd16(const uint8_t* b){ return ((uint32_t)b[0]<<8)|b[1]; }

static const uint32_t FRAME = 0x8000;

static int lzx_stream(uint32_t window, const uint8_t* in, size_t inlen, uint8_t* out, size_t outlen){
  mspack_system sys; memset(&sys,0,sizeof(sys));
  sys.read=m_read; sys.write=m_write; sys.alloc=m_alloc; sys.free=m_free; sys.copy=m_copy;
  memfile mi={(off_t)inlen,0,(uint8_t*)in}, mo={(off_t)outlen,0,out};
  uint32_t wb=0; { uint32_t w=window; while(w>1){ w>>=1; wb++; } }
  lzxd_stream* s=lzxd_init(&sys,(mspack_file*)&mi,(mspack_file*)&mo,(int)wb,0,0x8000,(off_t)outlen,0);
  if(!s) return -1;
  int rc=lzxd_decompress(s,(off_t)outlen);
  int full=(mo.off==(off_t)outlen);
  lzxd_free(s);
  return (rc==0 && full)?0:(rc?rc:-2);
}

int main(int argc, char** argv){
  if(argc<3){ fprintf(stderr,"usage: %s <in> <out>\n", argv[0]); return 2; }
  FILE* fi=fopen(argv[1],"rb"); if(!fi){perror("in");return 3;}
  _fseeki64(fi,0,SEEK_END); long long fsz=_ftelli64(fi); _fseeki64(fi,0,SEEK_SET);
  std::vector<uint8_t> data((size_t)fsz);
  if(fread(data.data(),1,(size_t)fsz,fi)!=(size_t)fsz){ fprintf(stderr,"short read\n"); return 3; }
  fclose(fi);
  if(fsz<16 || rd32(&data[0])!=0x0FF512ED || rd16(&data[4])!=0x0100){
    fprintf(stderr,"not XCTD (magic/version mismatch)\n"); return 4;
  }
  uint32_t fl   = rd32(&data[12]);
  uint32_t window = 1u << ((fl & 0xF) + 15);
  uint32_t zbs    = 0x8000u << ((fl >> 4) & 3);
  uint32_t segs   = (fl >> 6) & 0xFFFF;
  uint32_t bps    = ((fl >> 22) & 3) ? 32 : 20;

  FILE* fo=fopen(argv[2],"wb"); if(!fo){perror("out");return 3;}

  if(segs==0){ // raw payload (e.g. the 108-byte .dict files)
    size_t n=(size_t)fsz-16;
    fwrite(&data[16],1,n,fo); fclose(fo);
    fprintf(stderr,"raw: %lld -> %zu bytes (flags 0x%08x)\n",fsz,n,fl);
    printf("%zu\n",n);
    return 0;
  }

  // segment size table (bps bits per entry, MSB-first, padded to 32-bit words)
  std::vector<uint64_t> sizes(segs);
  size_t table_words=((uint64_t)bps*segs+31)/32;
  size_t bdo=16+4*table_words;
  if(bdo>(size_t)fsz){ fprintf(stderr,"table exceeds file\n"); return 4; }
  for(uint32_t k=0;k<segs;k++){
    if(bps==32) sizes[k]=rd32(&data[16+4*(size_t)k]);
    else{
      uint64_t bit=(uint64_t)k*20, v=0;
      for(int i=0;i<20;i++){
        uint64_t b=bit+i;
        v=(v<<1)|((data[16+(b>>3)]>>(7-(b&7)))&1);
      }
      sizes[k]=v;
    }
  }

  // Two segment layouts exist in the wild:
  //  CONTIGUOUS (Captain America DATA.*, all single-segment bundles): one
  //    global chunk stream; segment k owns the next ceil(size/32K) chunks; a
  //    zero word is zero PADDING of arbitrary byte length (may be ODD) up to
  //    the next zbs boundary. Fully validatable: total chunks == total frames
  //    and the walk tiles to EOF.
  //  ZONED (XBLA titles like 'Splosion Man; QuickBMS "start = k*zbs"): segment
  //    k lives alone in file zone [k*zbs, (k+1)*zbs) (segment 0 starts at bdo),
  //    chunk-framed inside the zone; the zone TAIL after the segment's frames
  //    is padding of arbitrary GARBAGE bytes (not zeros!), so a global walk
  //    derails silently. Detected by the contiguous validation failing.
  auto frames_of=[&](uint32_t k)->size_t{ return (size_t)((sizes[k]+FRAME-1)/FRAME); };
  unsigned long long want=0;
  for(uint32_t k=0;k<segs;k++) want+=frames_of(k);

  // ---- try CONTIGUOUS: global walk with zero-pad rule ----
  std::vector<std::pair<size_t,uint32_t>> chunks;
  bool contiguous_ok=true;
  {
    size_t p=bdo;
    while(p+2<=(size_t)fsz){
      if(data[p]==0 && data[p+1]==0){
        size_t np=((p/zbs)+1)*zbs;
        if(np>(size_t)fsz) np=(size_t)fsz;
        bool allz=true;
        for(size_t q=p;q<np;q++) if(data[q]!=0){ allz=false; break; }
        if(!allz){ contiguous_ok=false; break; }
        p=np; continue;
      }
      uint32_t sz=rd16(&data[p]); p+=2;
      if(p+sz>(size_t)fsz){ contiguous_ok=false; break; }
      chunks.push_back({p,sz}); p+=sz;
    }
    if(contiguous_ok && p!=(size_t)fsz){
      size_t q=p; while(q<(size_t)fsz && data[q]==0) q++;
      if(q!=(size_t)fsz) contiguous_ok=false;
    }
    if(contiguous_ok && want!=chunks.size()) contiguous_ok=false;
  }

  // ---- fallback ZONED: re-collect chunks per zone ----
  if(!contiguous_ok){
    chunks.clear();
    size_t bad=0;
    for(uint32_t k=0;k<segs;k++){
      size_t zstart=(k==0)?bdo:(size_t)k*zbs;
      size_t zend=(size_t)(k+1)*zbs; if(zend>(size_t)fsz) zend=(size_t)fsz;
      size_t p=zstart;
      for(size_t j=0;j<frames_of(k);j++){
        if(p+2>zend){ fprintf(stderr,"zoned: segment %u frame %zu runs past its zone\n",k,j); return 6; }
        uint32_t sz=rd16(&data[p]); p+=2;
        if(sz==0 || p+sz>zend){ fprintf(stderr,"zoned: segment %u frame %zu bad chunk size %u at 0x%zx\n",k,j,sz,p-2); return 6; }
        chunks.push_back({p,sz}); p+=sz;
      }
      (void)bad;
    }
  }

  // decode segment streams and concatenate (identical for both layouts:
  // segment k owns chunks [cum(k), cum(k+1)) and decodes to exactly sizes[k])
  size_t ci=0; unsigned long long written=0;
  std::vector<uint8_t> in, out;
  for(uint32_t k=0;k<segs;k++){
    size_t need=frames_of(k);
    in.clear();
    for(size_t j=0;j<need;j++){ auto& c=chunks[ci+j]; in.insert(in.end(), &data[c.first], &data[c.first]+c.second); }
    ci+=need;
    out.resize((size_t)sizes[k]);
    // XMemCompress omits the CAB realign pad byte after odd-sized UNCOMPRESSED
    // blocks; retry the segment in that dialect when the default fails. The
    // switch is per-attempt, so streams with CAB semantics are unaffected.
    lzxd_xmem_no_uncompressed_realign=0;
    int rc=lzx_stream(window,in.data(),in.size(),out.data(),(size_t)sizes[k]);
    if(rc){
      lzxd_xmem_no_uncompressed_realign=1;
      rc=lzx_stream(window,in.data(),in.size(),out.data(),(size_t)sizes[k]);
      lzxd_xmem_no_uncompressed_realign=0;
    }
    if(rc){ fprintf(stderr,"segment %u/%u decode failed rc=%d (size %llu, layout %s)\n",
                    k,segs,rc,(unsigned long long)sizes[k],contiguous_ok?"contiguous":"zoned"); return 5; }
    fwrite(out.data(),1,(size_t)sizes[k],fo); written+=sizes[k];
  }
  fclose(fo);
  fprintf(stderr,"%u segment(s), window 0x%x, zbs 0x%x, %s: %lld -> %llu bytes\n",
          segs,window,zbs,contiguous_ok?"contiguous":"zoned",fsz,written);
  printf("%llu\n",written);
  return 0;
}
