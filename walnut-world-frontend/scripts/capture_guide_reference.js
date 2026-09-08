async (page) => {
  await page.setViewportSize({width:1280,height:720});
  await page.goto('http://127.0.0.1:4886/demo.html?embed=1&still=1');
  const pages = await page.evaluate(() => GUIDE_SCENES.map(s => s.id));
  const states = {E02:'selected',S09:'submitting'};
  for (const id of pages) {
    await page.evaluate(({id,state}) => __DEMO__.show(id,state), {id,state:states[id]||'default'});
    await page.evaluate(async () => {
      await document.fonts.ready;
      await Promise.all([...document.querySelectorAll('#stage img')].map(i=>i.decode()));
    });
    await page.locator('#stage').screenshot({path:`walnut-world-frontend/docs/design/verification/guide-alignment/reference-${id}.png`});
  }
  return {captured:pages.length,source:'V2.2 guide live demo',viewport:[1280,720]};
}
