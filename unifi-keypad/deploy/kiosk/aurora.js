/* A small, local Canvas animation. No images, network calls, or GPU shader. */
(()=>{
  const canvas=document.getElementById('aurora-ring'),orb=document.getElementById('aurora-orb'),screen=document.getElementById('recovery');
  const ctx=canvas?.getContext('2d');if(!ctx)return;
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  let request=0,last=0,size=300,scale=1,touched=-100,origin=performance.now();
  function resize(){const box=canvas.getBoundingClientRect();size=Math.max(1,box.width);scale=Math.min(devicePixelRatio||1,1.5);canvas.width=Math.round(size*scale);canvas.height=Math.round(size*scale);draw((performance.now()-origin)/1000);}
  function draw(t){
    if(reduced.matches)t=0;
    ctx.setTransform(scale,0,0,scale,0,0);ctx.clearRect(0,0,size,size);
    const c=size/2,breath=1+.037*Math.sin(t*.95),pulse=Math.max(0,1-(t-touched)/1.5),r=size*.325*(breath+pulse*.045),hue=(218+t*20)%360;
    const glow=ctx.createRadialGradient(c,c,r*.52,c,c,r*1.5);
    glow.addColorStop(0,`hsla(${hue+35},90%,66%,0)`);glow.addColorStop(.48,`hsla(${hue+35},90%,66%,.035)`);glow.addColorStop(.65,`hsla(${hue},100%,70%,.065)`);glow.addColorStop(1,`hsla(${hue},100%,70%,0)`);
    ctx.fillStyle=glow;ctx.fillRect(0,0,size,size);ctx.globalCompositeOperation='lighter';
    const gradient=ctx.createLinearGradient(c-r,c-r,c+r,c+r);
    for(let n=0;n<=6;n++)gradient.addColorStop(n/6,`hsla(${hue+n*39},95%,${69+n%2*8}%,.8)`);
    for(let layer=0;layer<7;layer++){
      ctx.beginPath();const drift=layer*.73;
      for(let n=0;n<=168;n++){
        const a=n/168*Math.PI*2,undulation=(Math.sin(a*3+t*.55+drift)*2.6+Math.sin(a*5-t*.38+drift)*1.7+Math.sin(a*9+t*.25)*.55)*size/300;
        const radius=r+(layer-3)*size*.007+undulation,x=c+Math.cos(a)*radius,y=c+Math.sin(a)*radius;
        if(n===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
      }
      ctx.closePath();ctx.lineWidth=(layer===2||layer===4?1.9:.7)*size/300;ctx.strokeStyle=gradient;ctx.globalAlpha=layer===2?.95:layer===4?.7:.32;ctx.shadowColor=`hsl(${hue+layer*18},95%,70%)`;ctx.shadowBlur=layer===2?size*.018:0;ctx.stroke();
    }
    ctx.shadowBlur=0;
    for(let n=0;n<4;n++){
      const angle=t*(n%2?-.09:.11)+n*Math.PI*.58,rr=r+size*(.025+n*.006);
      ctx.beginPath();ctx.arc(c,c,rr,angle,angle+.28+n*.04);ctx.lineWidth=size*.0025;ctx.globalAlpha=.22+.15*Math.sin(t*.7+n)**2;ctx.strokeStyle=`hsl(${hue+n*52},95%,80%)`;ctx.stroke();
    }
    ctx.globalAlpha=1;ctx.globalCompositeOperation='source-over';
  }
  function tick(now){request=0;if(document.hidden||screen.hidden)return;if(now-last>=32){draw((now-origin)/1000);last=now;}if(!reduced.matches)request=requestAnimationFrame(tick);}
  function start(){if(!request&&!document.hidden&&!screen.hidden){if(reduced.matches)draw(0);else request=requestAnimationFrame(tick);}}
  document.body.classList.add('aurora-ready');
  addEventListener('resize',resize);document.addEventListener('visibilitychange',start);
  reduced.addEventListener('change',()=>{if(request)cancelAnimationFrame(request);request=0;draw(0);start();});
  orb.addEventListener('pointerdown',()=>{touched=(performance.now()-origin)/1000;start();},{passive:true});
  window.astraAurora={start};resize();start();
})();
