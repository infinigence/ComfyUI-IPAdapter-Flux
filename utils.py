import torch
from torch import Tensor
from .flux.layers import DoubleStreamBlockIPA, SingleStreamBlockIPA
from comfy.ldm.flux.layers import timestep_embedding
from types import MethodType

def FluxUpdateModules(bi, ip_attn_procs, image_emb, is_patched):
    flux_model = bi.model
    bi.add_object_patch(f"diffusion_model.forward_orig",MethodType(forward_orig_ipa,flux_model.diffusion_model))
    dsb_count = len(flux_model.diffusion_model.double_blocks)
    ssb_count = len(flux_model.diffusion_model.single_blocks)
    for i in range(dsb_count):
        patch_name = f"double_blocks.{i}"
        existing_layer = bi.get_model_object(f"diffusion_model.{patch_name}")
        if isinstance(existing_layer, DoubleStreamBlockIPA):
            # add a second ipadapter to the block
            existing_layer.add_adapter(ip_attn_procs[patch_name], image_emb)
        else:
            # initial ipa models with image embeddings
            temp_layer = DoubleStreamBlockIPA(flux_model.diffusion_model.double_blocks[i], ip_attn_procs[patch_name], image_emb)
            # TODO: maybe there's a different patching method that will automatically chain patches?
            # for example, ComfyUI internally uses model.add_patches to add loras
            bi.add_object_patch(f"diffusion_model.{patch_name}", temp_layer)
    for i in range(ssb_count):
        patch_name = f"single_blocks.{i}"
        existing_layer = bi.get_model_object(f"diffusion_model.{patch_name}")
        if isinstance(existing_layer, SingleStreamBlockIPA):
            existing_layer.add_adapter(ip_attn_procs[patch_name], image_emb)
        else:
            # initial ipa models with image embeddings
            temp_layer = SingleStreamBlockIPA(flux_model.diffusion_model.single_blocks[i], ip_attn_procs[patch_name], image_emb)
            bi.add_object_patch(f"diffusion_model.{patch_name}", temp_layer)
        
def is_model_patched(model):
    def test(mod):
        if isinstance(mod, DoubleStreamBlockIPA):
            return True
        else:
            for p in mod.children():
                if test(p):
                    return True
        return False
    result = test(model)
    return result

def forward_orig_ipa(
    self,
    img: Tensor,
    img_ids: Tensor,
    txt: Tensor,
    txt_ids: Tensor,
    timesteps: Tensor,
    y: Tensor,
    guidance: Tensor|None = None,
    control=None,
    transformer_options={},
) -> Tensor:
    patches_replace = transformer_options.get("patches_replace", {})
    if img.ndim != 3 or txt.ndim != 3:
        raise ValueError("Input img and txt tensors must have 3 dimensions.")

    # running on sequences img
    img = self.img_in(img)
    vec = self.time_in(timestep_embedding(timesteps, 256).to(img.dtype))
    if self.params.guidance_embed:
        if guidance is None:
            raise ValueError("Didn't get guidance strength for guidance distilled model.")
        vec = vec + self.guidance_in(timestep_embedding(guidance, 256).to(img.dtype))

    vec = vec + self.vector_in(y[:,:self.params.vec_in_dim])
    txt = self.txt_in(txt)

    ids = torch.cat((txt_ids, img_ids), dim=1)
    pe = self.pe_embedder(ids)

    blocks_replace = patches_replace.get("dit", {})
    for i, block in enumerate(self.double_blocks):
        if ("double_block", i) in blocks_replace:
            def block_wrap(args):
                out = {}
                if isinstance(block, DoubleStreamBlockIPA): # ipadaper 
                    out["img"], out["txt"] = block(img=args["img"], txt=args["txt"], vec=args["vec"], pe=args["pe"], t=args["timesteps"])
                else:
                    out["img"], out["txt"] = block(img=args["img"], txt=args["txt"], vec=args["vec"], pe=args["pe"])
                return out
            out = blocks_replace[("double_block", i)]({"img": img, "txt": txt, "vec": vec, "pe": pe, "timesteps": timesteps}, {"original_block": block_wrap})
            txt = out["txt"]
            img = out["img"]
        else:
            if isinstance(block, DoubleStreamBlockIPA): # ipadaper 
                img, txt = block(img=img, txt=txt, vec=vec, pe=pe, t=timesteps)
            else:
                img, txt = block(img=img, txt=txt, vec=vec, pe=pe)

        if control is not None: # Controlnet
            control_i = control.get("input")
            if i < len(control_i):
                add = control_i[i]
                if add is not None:
                    img += add

    img = torch.cat((txt, img), 1)

    for i, block in enumerate(self.single_blocks):
        if ("single_block", i) in blocks_replace:
            def block_wrap(args):
                out = {}
                if isinstance(block, SingleStreamBlockIPA): # ipadaper
                    out["img"] = block(args["img"], vec=args["vec"], pe=args["pe"], t=args["timesteps"])
                else:
                    out["img"] = block(args["img"], vec=args["vec"], pe=args["pe"])
                return out

            out = blocks_replace[("single_block", i)]({"img": img, "vec": vec, "pe": pe, "timesteps": timesteps}, {"original_block": block_wrap})
            img = out["img"]
        else:
            if isinstance(block, SingleStreamBlockIPA): # ipadaper
                img = block(img, vec=vec, pe=pe, t=timesteps)
            else:
                img = block(img, vec=vec, pe=pe)

        if control is not None: # Controlnet
            control_o = control.get("output")
            if i < len(control_o):
                add = control_o[i]
                if add is not None:
                    img[:, txt.shape[1] :, ...] += add

    img = img[:, txt.shape[1] :, ...]

    img = self.final_layer(img, vec)  # (N, T, patch_size ** 2 * out_channels)
    return img
